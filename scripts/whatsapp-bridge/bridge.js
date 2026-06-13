#!/usr/bin/env node
/**
 * Hermes Agent WhatsApp Bridge
 *
 * Standalone Node.js process that connects to WhatsApp via Baileys
 * and exposes HTTP endpoints for the Python gateway adapter.
 *
 * Endpoints (matches gateway/platforms/whatsapp.py expectations):
 *   GET  /messages       - Long-poll for new incoming messages
 *   POST /send           - Send a message { chatId, message, replyTo? }
 *   POST /edit           - Edit a sent message { chatId, messageId, message }
 *   POST /send-media     - Send media natively { chatId, filePath, mediaType?, caption?, fileName? }
 *   POST /typing         - Send typing indicator { chatId }
 *   GET  /chat/:id       - Get chat info
 *   GET  /health         - Health check
 *
 * Usage:
 *   node bridge.js --port 3000 --session ~/.hermes/whatsapp/session
 */

import { makeWASocket, useMultiFileAuthState, DisconnectReason, fetchLatestBaileysVersion, downloadMediaMessage } from '@whiskeysockets/baileys';
import express from 'express';
import { Boom } from '@hapi/boom';
import pino from 'pino';
import path from 'path';
import { mkdirSync, readFileSync, writeFileSync, existsSync, readdirSync, unlinkSync } from 'fs';
import { fileURLToPath } from 'url';
import { randomBytes, createHash } from 'crypto';
import { execSync } from 'child_process';
import { tmpdir } from 'os';
import qrcode from 'qrcode-terminal';
// ``qrcode`` (no -terminal) is used to render the QR as a PNG data URL
// for the dashboard's <img> tag. Resolved from the root
// ``node_modules`` since the bridge script lives in a subdirectory but
// the package is hoisted to the workspace root.
import qrcodeImage from 'qrcode';
import { matchesAllowedUser, parseAllowedUsers } from './allowlist.js';

// Parse CLI args. Supports both `--name value` and `--name=value` forms
// (the Python gateway prefers `=` for shell-safe quoting, while the
// local CLI historically used the space form — keep both working).
const args = process.argv.slice(2);
function getArg(name, defaultVal) {
  const eq = args.find((a) => a.startsWith(`--${name}=`));
  if (eq) return eq.split('=').slice(1).join('=');
  const idx = args.indexOf(`--${name}`);
  return idx !== -1 && args[idx + 1] ? args[idx + 1] : defaultVal;
}

const WHATSAPP_DEBUG =
  typeof process !== 'undefined' &&
  process.env &&
  typeof process.env.WHATSAPP_DEBUG === 'string' &&
  ['1', 'true', 'yes', 'on'].includes(process.env.WHATSAPP_DEBUG.toLowerCase());

const PORT = parseInt(getArg('port', '3000'), 10);
const SESSION_DIR = getArg('session', path.join(process.env.HOME || '~', '.hermes', 'whatsapp', 'session'));
// Cache directories: the Python gateway passes the profile-aware paths via
// env (HERMES_HOME-aware, new cache/ layout).  Fall back to the legacy
// hardcoded locations for bridges launched outside the gateway.
const IMAGE_CACHE_DIR = process.env.HERMES_IMAGE_CACHE_DIR
  || path.join(process.env.HOME || '~', '.hermes', 'image_cache');
const DOCUMENT_CACHE_DIR = process.env.HERMES_DOCUMENT_CACHE_DIR
  || path.join(process.env.HOME || '~', '.hermes', 'document_cache');
const AUDIO_CACHE_DIR = process.env.HERMES_AUDIO_CACHE_DIR
  || path.join(process.env.HOME || '~', '.hermes', 'audio_cache');

// Self-hash of this script file.  Reported in /health so the Python gateway
// can detect a running bridge that predates the current bridge.js and
// restart it instead of silently reusing stale code (stale-bridge trap:
// `hermes update` updates bridge.js on disk but a long-lived bridge process
// keeps serving the old behavior forever).
let SCRIPT_HASH = '';
try {
  SCRIPT_HASH = createHash('sha256')
    .update(readFileSync(fileURLToPath(import.meta.url)))
    .digest('hex')
    .slice(0, 16);
} catch {}
const PAIR_ONLY = args.includes('--pair-only');
const WHATSAPP_MODE = getArg('mode', process.env.WHATSAPP_MODE || 'self-chat'); // "bot" or "self-chat"
const ALLOWED_USERS = parseAllowedUsers(process.env.WHATSAPP_ALLOWED_USERS || '');
const DEFAULT_REPLY_PREFIX = '⚕ *Hermes Agent*\n────────────\n';
const REPLY_PREFIX = process.env.WHATSAPP_REPLY_PREFIX === undefined
  ? DEFAULT_REPLY_PREFIX
  : process.env.WHATSAPP_REPLY_PREFIX.replace(/\\n/g, '\n');
const MAX_MESSAGE_LENGTH = parseInt(process.env.WHATSAPP_MAX_MESSAGE_LENGTH || '4096', 10);
const CHUNK_DELAY_MS = parseInt(process.env.WHATSAPP_CHUNK_DELAY_MS || '300', 10);
// Per-call timeout for sock.sendMessage(). Baileys occasionally hangs forever
// when uploading media to WhatsApp servers (and, less often, on text sends),
// which pins the bridge's HTTP handler until the upstream aiohttp timeout
// fires. Fail fast instead so the gateway can surface a real error and retry.
const SEND_TIMEOUT_MS = parseInt(process.env.WHATSAPP_SEND_TIMEOUT_MS || '60000', 10);

function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

function sendWithTimeout(chatId, payload, timeoutMs = SEND_TIMEOUT_MS) {
  let timer;
  const timeoutPromise = new Promise((_, reject) => {
    timer = setTimeout(
      () => reject(new Error(`sendMessage timed out after ${timeoutMs / 1000}s`)),
      timeoutMs,
    );
  });
  return Promise.race([sock.sendMessage(chatId, payload), timeoutPromise])
    .finally(() => clearTimeout(timer));
}

function formatOutgoingMessage(message) {
  // In bot mode, messages come from a different number so the prefix is
  // redundant — the sender identity is already clear.  Only prepend in
  // self-chat mode where bot and user share the same number.
  if (WHATSAPP_MODE !== 'self-chat') return message;
  return REPLY_PREFIX ? `${REPLY_PREFIX}${message}` : message;
}

function splitLongMessage(message, maxLength = MAX_MESSAGE_LENGTH) {
  const text = String(message || '');
  if (!text) return [];
  if (!Number.isFinite(maxLength) || maxLength < 1 || text.length <= maxLength) {
    return [text];
  }

  const chunks = [];
  let remaining = text;
  while (remaining.length > maxLength) {
    let splitAt = remaining.lastIndexOf('\n', maxLength);
    if (splitAt < Math.floor(maxLength / 2)) {
      splitAt = remaining.lastIndexOf(' ', maxLength);
    }
    if (splitAt < 1) splitAt = maxLength;

    chunks.push(remaining.slice(0, splitAt).trimEnd());
    remaining = remaining.slice(splitAt).trimStart();
  }
  if (remaining) chunks.push(remaining);
  return chunks;
}

function trackSentMessageId(sent) {
  if (sent?.key?.id) {
    recentlySentIds.add(sent.key.id);
    if (recentlySentIds.size > MAX_RECENT_IDS) {
      recentlySentIds.delete(recentlySentIds.values().next().value);
    }
  }
}

function normalizeWhatsAppId(value) {
  if (!value) return '';
  return String(value).replace(':', '@');
}

function getMessageContent(msg) {
  const content = msg?.message || {};
  if (content.ephemeralMessage?.message) return content.ephemeralMessage.message;
  if (content.viewOnceMessage?.message) return content.viewOnceMessage.message;
  if (content.viewOnceMessageV2?.message) return content.viewOnceMessageV2.message;
  if (content.documentWithCaptionMessage?.message) return content.documentWithCaptionMessage.message;
  if (content.templateMessage?.hydratedTemplate) return content.templateMessage.hydratedTemplate;
  if (content.buttonsMessage) return content.buttonsMessage;
  if (content.listMessage) return content.listMessage;
  return content;
}

function getContextInfo(messageContent) {
  if (!messageContent || typeof messageContent !== 'object') return {};
  for (const value of Object.values(messageContent)) {
    if (value && typeof value === 'object' && value.contextInfo) {
      return value.contextInfo;
    }
  }
  return {};
}

mkdirSync(SESSION_DIR, { recursive: true });

// Build LID → phone reverse map from session files (lid-mapping-{phone}.json)
function buildLidMap() {
  const map = {};
  try {
    for (const f of readdirSync(SESSION_DIR)) {
      const m = f.match(/^lid-mapping-(\d+)\.json$/);
      if (!m) continue;
      const phone = m[1];
      const lid = JSON.parse(readFileSync(path.join(SESSION_DIR, f), 'utf8'));
      if (lid) map[String(lid)] = phone;
    }
  } catch {}
  return map;
}
let lidToPhone = buildLidMap();

const logger = pino({ level: 'warn' });

// Message queue for polling
const messageQueue = [];
const MAX_QUEUE_SIZE = 100;

// Track recently sent message IDs to prevent echo-back loops with media
const recentlySentIds = new Set();
const MAX_RECENT_IDS = 50;

let sock = null;
let connectionState = 'disconnected';
// Pairing state (drives /health + /pairing-code + JSON events to stdout).
// Populated when --phone is supplied and Baileys emits a QR; cleared on
// cancel.  Surfaced via the dashboard so it can render the 8-char code
// without scraping the bridge.log file.
let pairingCode = '';
let pairingPhone = '';
let pairingError = '';
// QR data URL (PNG base64) for QR mode. Refreshed on every QR emit so
// the dashboard's <img> tag stays in sync with whatever WA's QR is
// currently showing. Cleared on successful pairing or cancel.
let pairingQrDataUrl = '';
let pairedAt = 0; // ms epoch — non-zero once connection === 'open' with creds
let connectionEverOpen = false; // true once the socket has reached 'open' state at least once
let pairingComplete = false; // true after a successful pairing in --pair-only mode
// Reconnect bookkeeping. WhatsApp's anti-spam throttles devices that
// hammer the auth endpoints, so a blanket ``setTimeout(startSocket, 2000)``
// on every disconnect can land the device in a temporary ban. We:
//   1. Cap the number of reconnect attempts within a window
//   2. Apply exponential backoff (2s, 4s, 8s, 16s, 32s, capped at 60s)
//   3. In ``--pair-only`` mode we *never* auto-reconnect — a disconnect
//      before the user finished entering the code means the code is
//      dead anyway, so the bridge exits and the dashboard can re-spawn.
let reconnectAttempts = 0;
let lastReconnectAt = 0;
const RECONNECT_MAX_IN_WINDOW = 4;
const RECONNECT_WINDOW_MS = 60_000;
// Mode is set at boot: 'phone' (--phone=X) or 'qr' (default when no --phone).
// Used to decide which dashboard UI to render and to skip the pairing
// code request when in QR mode (avoids the 3s pre-code delay).
const PAIRING_MODE = process.argv.find((a) => a.startsWith('--phone=')) ? 'phone' : 'qr';

async function startSocket() {
  const { state, saveCreds } = await useMultiFileAuthState(SESSION_DIR);
  const { version } = await fetchLatestBaileysVersion();

  sock = makeWASocket({
    version,
    auth: state,
    logger,
    printQRInTerminal: false,
    browser: ['Hermes Agent', 'Chrome', '120.0'],
    syncFullHistory: false,
    markOnlineOnConnect: false,
    // Required for Baileys 7.x: without this, incoming messages that need
    // E2EE session re-establishment are silently dropped (msg.message === null)
    getMessage: async (key) => {
      // We don't maintain a message store, so return a placeholder.
      // This is enough for Baileys to complete the retry handshake.
      return { conversation: '' };
    },
  });

  sock.ev.on('creds.update', () => { saveCreds(); lidToPhone = buildLidMap(); });

  sock.ev.on('connection.update', async (update) => {
    const { connection, lastDisconnect, qr } = update;

    if (qr) {
      console.log('\n📱 Scan this QR code with WhatsApp on your phone:\n');
      qrcode.generate(qr, { small: true });
      console.log('\nWaiting for scan...\n');

      // Render the same QR as a PNG data URL for the dashboard's
      // <img src=...> tag. Done in addition to the terminal print so
      // the user can scan from their phone without needing terminal
      // access. Re-emitted on every QR refresh (Baileys rotates QRs
      // ~ every 60s).
      try {
        // ``qrcode`` is the same package; ``toDataURL`` returns a
        // base64 PNG without writing to disk. ``small: true`` keeps the
        // payload small enough to ship in a /health poll every 2s.
        pairingQrDataUrl = await qrcodeImage.toDataURL(qr, {
          errorCorrectionLevel: 'M',
          margin: 1,
          scale: 6,
          color: { dark: '#000000', light: '#ffffff' },
        });
      } catch (err) {
        // Non-fatal: the terminal print is still the source of truth.
        console.error('⚠️ Failed to render QR data URL:', err);
      }

      // Pairing code fallback logic (only in 'phone' mode). In 'qr' mode
      // the user scans the QR from the dashboard, no code is needed —
      // skipping the requestPairingCode call also avoids the 3s sleep
      // blocking the QR stream.
      if (PAIRING_MODE === 'phone') {
        const phoneArg = process.argv.find(arg => arg.startsWith('--phone='));
        if (phoneArg) {
          const phone = phoneArg.split('=')[1];
          pairingPhone = phone;
          console.log(`📱 Requesting pairing code for phone: ${phone}`);
          try {
            // Wait 3s to let the socket establish connection to WA servers before requesting code
            await new Promise(resolve => setTimeout(resolve, 3000));
            const code = await sock.requestPairingCode(phone);
            const formatted = `${code.slice(0,4)}-${code.slice(4)}`;
            pairingCode = formatted;
            pairingError = '';
            console.log(`\n🔑 YOUR PAIRING CODE IS: ${formatted}\n`);
            // Structured event for the dashboard backend (no more screen-dump scraping).
            try {
              console.log(JSON.stringify({
                event: 'pairing_code',
                phone,
                code: formatted,
              }));
            } catch {}
          } catch (err) {
            pairingError = String(err?.message || err);
            console.error('⚠️ Failed to request pairing code:', err);
            try {
              console.log(JSON.stringify({ event: 'pairing_error', phone, error: pairingError }));
            } catch {}
          }
        }
      }
    }

    if (connection === 'close') {
      const reason = new Boom(lastDisconnect?.error)?.output?.statusCode;
      connectionState = 'disconnected';

      // Pairing-mode 401 grace period: in ``--pair-only`` mode, transient
      // 401s happen frequently *before* the user has finished pairing
      // (e.g. the device-revoked check fires once on the very first
      // connection attempt with empty creds, or the user types a wrong
      // / expired code and WA rejects the session). Treating those as a
      // hard logout would wipe the session mid-pairing and exit the
      // bridge — the user would never be able to retry without
      // restarting the dashboard.
      //
      // Only escalate to "logged out → wipe session → exit" once the
      // connection has been ``open`` at least once (i.e. pairing
      // succeeded) and then a *subsequent* 401 arrives. Pre-pairing
      // 401s are logged and ignored; the bridge keeps waiting for the
      // user to enter the code.
      const everConnected = connectionEverOpen || pairingComplete;
      const hardLogout =
        reason === DisconnectReason.loggedOut ||
        reason === 401 ||
        (lastDisconnect?.error?.output?.payload?.message || '').includes('device_removed');

      if (hardLogout && (everConnected || !PAIR_ONLY)) {
        console.log('❌ Logged out (401/device_removed). Cleaning session directory...');
        try {
          // Clear session files to prevent loop reconnecting with stale creds
          const files = readdirSync(SESSION_DIR);
          for (const file of files) {
            unlinkSync(path.join(SESSION_DIR, file));
          }
          console.log('🧹 Session cleared successfully.');
        } catch (err) {
          console.error('⚠️ Failed to clean session directory:', err);
        }
        process.exit(1);
      } else if (hardLogout) {
        // Pre-pairing 401 in --pair-only mode. Two scenarios:
        //   1. Fresh session: a 401 here is WA's anti-spam kicking in.
        //      Auto-reconnecting would loop the bridge into a ban, so
        //      we exit and let the dashboard respawn a clean process.
        //   2. Stale code entered: the code is dead, the user must
        //      restart pairing. Exiting is the right move there too.
        // In both cases, no re-arm, no reconnect — the dashboard
        // surfaces "Pairing bridge exited" and the user can retry
        // manually after a backoff.
        console.log(
          `⚠️  Pre-pairing ${reason} — exiting to avoid WA anti-spam loop. ` +
          'User must restart pairing from the dashboard.',
        );
        process.exit(1);
      } else {
        // Non-401 disconnect. Two cases that need different treatment:
        //   515 = server-requested restart (common right after pairing
        //         succeeds but before the app is fully ready). Always
        //         reconnect, but with a small fixed delay.
        //   Anything else = network blip, server overload, etc. Use
        //         exponential backoff and abort after RECONNECT_MAX_IN_WINDOW
        //         attempts inside RECONNECT_WINDOW_MS to stay clear of
        //         WA's anti-spam threshold.
        if (reason === 515) {
          console.log('↻ WhatsApp requested restart (code 515). Reconnecting...');
          setTimeout(startSocket, 1000);
        } else {
          const now = Date.now();
          if (now - lastReconnectAt > RECONNECT_WINDOW_MS) {
            // Window expired — reset counter so a long-stable connection
            // followed by a single blip doesn't carry old attempts.
            reconnectAttempts = 0;
          }
          reconnectAttempts += 1;
          lastReconnectAt = now;
          if (reconnectAttempts > RECONNECT_MAX_IN_WINDOW) {
            console.error(
              `❌ Reconnect cap hit (${reconnectAttempts} attempts in ` +
              `${RECONNECT_WINDOW_MS / 1000}s). Exiting to avoid WA ban.`,
            );
            process.exit(2);
          }
          // Exponential backoff: 2s, 4s, 8s, 16s, 32s, capped at 60s.
          const delay = Math.min(60_000, 2000 * 2 ** (reconnectAttempts - 1));
          console.log(
            `⚠️  Connection closed (reason: ${reason}). ` +
            `Reconnect attempt ${reconnectAttempts}/${RECONNECT_MAX_IN_WINDOW} ` +
            `in ${delay / 1000}s...`,
          );
          setTimeout(startSocket, delay);
        }
      }
    } else if (connection === 'open') {
      connectionState = 'connected';
      connectionEverOpen = true;
      // Reset the reconnect budget on a successful open so the next
      // disconnect (e.g. hours later after a stable session) starts
      // fresh instead of inheriting old attempt counts.
      reconnectAttempts = 0;
      console.log('✅ WhatsApp connected!');
      try {
        console.log(JSON.stringify({
          event: 'paired',
          phone: pairingPhone,
          user: sock?.user?.id || '',
        }));
      } catch {}
      if (PAIR_ONLY) {
        console.log('✅ Pairing complete. Credentials saved. Staying alive for dashboard apply/cancel.');
        // Stay alive (don't exit) so the dashboard can confirm the paired
        // state via /health, then either kill this process on apply or
        // leave it running.  The platform adapter's next connect() will
        // pick up the persisted creds.json and start a full bridge.
        pairedAt = Date.now();
        pairingComplete = true;
      }
    }
  });

  sock.ev.on('messages.upsert', async ({ messages, type }) => {
    // In self-chat mode, your own messages commonly arrive as 'append' rather
    // than 'notify'. Accept both and filter agent echo-backs below.
    if (type !== 'notify' && type !== 'append') return;

    const botIds = Array.from(new Set([
      normalizeWhatsAppId(sock.user?.id),
      normalizeWhatsAppId(sock.user?.lid),
    ].filter(Boolean)));

    for (const msg of messages) {
      if (!msg.message) continue;

      const chatId = msg.key.remoteJid;
      if (WHATSAPP_DEBUG) {
        try {
          console.log(JSON.stringify({
            event: 'upsert', type,
            fromMe: !!msg.key.fromMe, chatId,
            senderId: msg.key.participant || chatId,
            messageKeys: Object.keys(msg.message || {}),
          }));
        } catch {}
      }
      const senderId = msg.key.participant || chatId;
      const isGroup = chatId.endsWith('@g.us');
      const senderNumber = senderId.replace(/@.*/, '');

      // Handle fromMe messages based on mode
      if (msg.key.fromMe) {
        if (isGroup || chatId.includes('status')) continue;

        if (WHATSAPP_MODE === 'bot') {
          // Bot mode: separate number. ALL fromMe are echo-backs of our own replies — skip.
          continue;
        }

        // Self-chat mode: only allow messages in the user's own self-chat
        // WhatsApp now uses LID (Linked Identity Device) format: 67427329167522@lid
        // AND classic format: 34652029134@s.whatsapp.net
        // sock.user has both: { id: "number:10@s.whatsapp.net", lid: "lid_number:10@lid" }
        const myNumber = (sock.user?.id || '').replace(/:.*@/, '@').replace(/@.*/, '');
        const myLid = (sock.user?.lid || '').replace(/:.*@/, '@').replace(/@.*/, '');
        const chatNumber = chatId.replace(/@.*/, '');
        const isSelfChat = (myNumber && chatNumber === myNumber) || (myLid && chatNumber === myLid);
        if (!isSelfChat) continue;
      }

      // Handle !fromMe messages (from other people) based on mode.
      // Self-chat mode only responds to the user's own messages to
      // themselves — stranger DMs / group pings must never reach the
      // Python gateway, otherwise a pairing-code reply fires in response
      // to arbitrary incoming messages (#8389).
      if (!msg.key.fromMe) {
        if (WHATSAPP_MODE === 'self-chat') {
          try {
            console.log(JSON.stringify({
              event: 'ignored',
              reason: 'self_chat_mode_rejects_non_self',
              chatId,
              senderId,
            }));
          } catch {}
          continue;
        }
        if (!matchesAllowedUser(senderId, ALLOWED_USERS, SESSION_DIR)) {
          try {
            console.log(JSON.stringify({
              event: 'ignored',
              reason: 'allowlist_mismatch',
              chatId,
              senderId,
            }));
          } catch {}
          continue;
        }
      }

      const messageContent = getMessageContent(msg);
      const contextInfo = getContextInfo(messageContent);
      const mentionedIds = Array.from(new Set((contextInfo?.mentionedJid || []).map(normalizeWhatsAppId).filter(Boolean)));
      const quotedMessageId = contextInfo?.stanzaId || null;
      const quotedParticipant = normalizeWhatsAppId(contextInfo?.participant || '') || null;
      const quotedRemoteJid = normalizeWhatsAppId(contextInfo?.remoteJid || '') || null;
      const hasQuotedMessage = !!contextInfo?.quotedMessage;

      // Extract message body
      let body = '';
      let hasMedia = false;
      let mediaType = '';
      const mediaUrls = [];

      if (messageContent.conversation) {
        body = messageContent.conversation;
      } else if (messageContent.extendedTextMessage?.text) {
        body = messageContent.extendedTextMessage.text;
      } else if (messageContent.imageMessage) {
        body = messageContent.imageMessage.caption || '';
        hasMedia = true;
        mediaType = 'image';
        try {
          const buf = await downloadMediaMessage(msg, 'buffer', {}, { logger, reuploadRequest: sock.updateMediaMessage });
          const mime = messageContent.imageMessage.mimetype || 'image/jpeg';
          const extMap = { 'image/jpeg': '.jpg', 'image/png': '.png', 'image/webp': '.webp', 'image/gif': '.gif' };
          const ext = extMap[mime] || '.jpg';
          mkdirSync(IMAGE_CACHE_DIR, { recursive: true });
          const filePath = path.join(IMAGE_CACHE_DIR, `img_${randomBytes(6).toString('hex')}${ext}`);
          writeFileSync(filePath, buf);
          mediaUrls.push(filePath);
        } catch (err) {
          console.error('[bridge] Failed to download image:', err.message);
        }
      } else if (messageContent.videoMessage) {
        body = messageContent.videoMessage.caption || '';
        hasMedia = true;
        mediaType = 'video';
        try {
          const buf = await downloadMediaMessage(msg, 'buffer', {}, { logger, reuploadRequest: sock.updateMediaMessage });
          const mime = messageContent.videoMessage.mimetype || 'video/mp4';
          const ext = mime.includes('mp4') ? '.mp4' : '.mkv';
          mkdirSync(DOCUMENT_CACHE_DIR, { recursive: true });
          const filePath = path.join(DOCUMENT_CACHE_DIR, `vid_${randomBytes(6).toString('hex')}${ext}`);
          writeFileSync(filePath, buf);
          mediaUrls.push(filePath);
        } catch (err) {
          console.error('[bridge] Failed to download video:', err.message);
        }
      } else if (messageContent.audioMessage || messageContent.pttMessage) {
        hasMedia = true;
        mediaType = messageContent.pttMessage ? 'ptt' : 'audio';
        try {
          const audioMsg = messageContent.pttMessage || messageContent.audioMessage;
          const buf = await downloadMediaMessage(msg, 'buffer', {}, { logger, reuploadRequest: sock.updateMediaMessage });
          const mime = audioMsg.mimetype || 'audio/ogg';
          const ext = mime.includes('ogg') ? '.ogg' : mime.includes('mp4') ? '.m4a' : '.ogg';
          mkdirSync(AUDIO_CACHE_DIR, { recursive: true });
          const filePath = path.join(AUDIO_CACHE_DIR, `aud_${randomBytes(6).toString('hex')}${ext}`);
          writeFileSync(filePath, buf);
          mediaUrls.push(filePath);
        } catch (err) {
          console.error('[bridge] Failed to download audio:', err.message);
        }
      } else if (messageContent.documentMessage) {
        body = messageContent.documentMessage.caption || '';
        hasMedia = true;
        mediaType = 'document';
        const fileName = messageContent.documentMessage.fileName || 'document';
        try {
          const buf = await downloadMediaMessage(msg, 'buffer', {}, { logger, reuploadRequest: sock.updateMediaMessage });
          mkdirSync(DOCUMENT_CACHE_DIR, { recursive: true });
          const safeFileName = path.basename(fileName).replace(/[^a-zA-Z0-9._-]/g, '_');
          const filePath = path.join(DOCUMENT_CACHE_DIR, `doc_${randomBytes(6).toString('hex')}_${safeFileName}`);
          writeFileSync(filePath, buf);
          mediaUrls.push(filePath);
        } catch (err) {
          console.error('[bridge] Failed to download document:', err.message);
        }
      }

      // For media without caption, use a placeholder so the API message is never empty
      if (hasMedia && !body) {
        body = `[${mediaType} received]`;
      }

      // Ignore Hermes' own reply messages in self-chat mode to avoid loops.
      if (msg.key.fromMe && ((REPLY_PREFIX && body.startsWith(REPLY_PREFIX)) || recentlySentIds.has(msg.key.id))) {
        if (WHATSAPP_DEBUG) {
          try { console.log(JSON.stringify({ event: 'ignored', reason: 'agent_echo', chatId, messageId: msg.key.id })); } catch {}
        }
        continue;
      }

      // Skip empty messages
      if (!body && !hasMedia) {
        if (WHATSAPP_DEBUG) {
          try { 
            console.log(JSON.stringify({ event: 'ignored', reason: 'empty', chatId, messageKeys: Object.keys(msg.message || {}) })); 
          } catch (err) {
            console.error('Failed to log empty message event:', err);
          }
        }
        continue;
      }

      const event = {
        messageId: msg.key.id,
        chatId,
        senderId,
        senderName: msg.pushName || senderNumber,
        chatName: isGroup ? (chatId.split('@')[0]) : (msg.pushName || senderNumber),
        isGroup,
        body,
        hasMedia,
        mediaType,
        mediaUrls,
        mentionedIds,
        quotedMessageId,
        quotedParticipant,
        quotedRemoteJid,
        hasQuotedMessage,
        botIds,
        timestamp: msg.messageTimestamp,
      };

      messageQueue.push(event);
      if (messageQueue.length > MAX_QUEUE_SIZE) {
        messageQueue.shift();
      }
    }
  });
}

// HTTP server
const app = express();
app.use(express.json());

// Host-header validation — defends against DNS rebinding.
// The bridge binds loopback-only (127.0.0.1) but a victim browser on
// the same machine could be tricked into fetching from an attacker
// hostname that TTL-flips to 127.0.0.1. Reject any request whose Host
// header doesn't resolve to a loopback alias.
// See GHSA-ppp5-vxwm-4cf7.
const _ACCEPTED_HOST_VALUES = new Set([
  'localhost',
  '127.0.0.1',
  '[::1]',
  '::1',
]);

app.use((req, res, next) => {
  const raw = (req.headers.host || '').trim();
  if (!raw) {
    return res.status(400).json({ error: 'Missing Host header' });
  }
  // Strip port suffix: "localhost:3000" → "localhost"
  const hostOnly = (raw.includes(':')
    ? raw.substring(0, raw.lastIndexOf(':'))
    : raw
  ).replace(/^\[|\]$/g, '').toLowerCase();
  if (!_ACCEPTED_HOST_VALUES.has(hostOnly)) {
    return res.status(400).json({
      error: 'Invalid Host header. Bridge accepts loopback hosts only.',
    });
  }
  next();
});

// Poll for new messages (long-poll style)
app.get('/messages', (req, res) => {
  const msgs = messageQueue.splice(0, messageQueue.length);
  res.json(msgs);
});

// Send a message
app.post('/send', async (req, res) => {
  if (!sock || connectionState !== 'connected') {
    return res.status(503).json({ error: 'Not connected to WhatsApp' });
  }

  const { chatId, message, replyTo } = req.body;
  if (!chatId || !message) {
    return res.status(400).json({ error: 'chatId and message are required' });
  }

  try {
    const chunks = splitLongMessage(formatOutgoingMessage(message));
    const messageIds = [];
    for (let i = 0; i < chunks.length; i += 1) {
      const sent = await sendWithTimeout(chatId, { text: chunks[i] });
      trackSentMessageId(sent);
      if (sent?.key?.id) messageIds.push(sent.key.id);
      if (chunks.length > 1 && i < chunks.length - 1) {
        await sleep(CHUNK_DELAY_MS);
      }
    }

    res.json({
      success: true,
      messageId: messageIds[messageIds.length - 1],
      messageIds,
    });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// Edit a previously sent message
app.post('/edit', async (req, res) => {
  if (!sock || connectionState !== 'connected') {
    return res.status(503).json({ error: 'Not connected to WhatsApp' });
  }

  const { chatId, messageId, message } = req.body;
  if (!chatId || !messageId || !message) {
    return res.status(400).json({ error: 'chatId, messageId, and message are required' });
  }

  try {
    const key = { id: messageId, fromMe: true, remoteJid: chatId };
    const chunks = splitLongMessage(formatOutgoingMessage(message));
    const messageIds = [];

    await sendWithTimeout(chatId, { text: chunks[0], edit: key });
    if (chunks.length > 1) {
      for (let i = 1; i < chunks.length; i += 1) {
        const sent = await sendWithTimeout(chatId, { text: chunks[i] });
        trackSentMessageId(sent);
        if (sent?.key?.id) messageIds.push(sent.key.id);
        if (i < chunks.length - 1) {
          await sleep(CHUNK_DELAY_MS);
        }
      }
    }

    res.json({ success: true, messageIds });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// MIME type map and media type inference for /send-media
const MIME_MAP = {
  jpg: 'image/jpeg', jpeg: 'image/jpeg', png: 'image/png',
  webp: 'image/webp', gif: 'image/gif',
  mp4: 'video/mp4', mov: 'video/quicktime', avi: 'video/x-msvideo',
  mkv: 'video/x-matroska', '3gp': 'video/3gpp',
  pdf: 'application/pdf',
  doc: 'application/msword',
  docx: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
  xlsx: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
};

function inferMediaType(ext) {
  if (['jpg', 'jpeg', 'png', 'webp', 'gif'].includes(ext)) return 'image';
  if (['mp4', 'mov', 'avi', 'mkv', '3gp'].includes(ext)) return 'video';
  if (['ogg', 'opus', 'mp3', 'wav', 'm4a'].includes(ext)) return 'audio';
  return 'document';
}

// Send media (image, video, document) natively
app.post('/send-media', async (req, res) => {
  if (!sock || connectionState !== 'connected') {
    return res.status(503).json({ error: 'Not connected to WhatsApp' });
  }

  const { chatId, filePath, mediaType, caption, fileName } = req.body;
  if (!chatId || !filePath) {
    return res.status(400).json({ error: 'chatId and filePath are required' });
  }

  try {
    if (!existsSync(filePath)) {
      return res.status(404).json({ error: `File not found: ${filePath}` });
    }

    const buffer = readFileSync(filePath);
    const ext = filePath.toLowerCase().split('.').pop();
    const type = mediaType || inferMediaType(ext);
    let msgPayload;

    switch (type) {
      case 'image':
        msgPayload = { image: buffer, caption: caption || undefined, mimetype: MIME_MAP[ext] || 'image/jpeg' };
        break;
      case 'video':
        msgPayload = { video: buffer, caption: caption || undefined, mimetype: MIME_MAP[ext] || 'video/mp4' };
        break;
      case 'audio': {
        // WhatsApp only renders a native voice bubble (ptt) when the file is ogg/opus.
        // If the caller passes mp3, wav, m4a etc. (e.g. from Edge TTS / NeuTTS),
        // silently convert to ogg/opus via ffmpeg so ptt is always honoured.
        let audioBuffer = buffer;
        let audioExt = ext;
        const needsConversion = !['ogg', 'opus'].includes(ext);
        let tmpPath = null;
        if (needsConversion) {
          tmpPath = path.join(tmpdir(), `hermes_voice_${randomBytes(6).toString('hex')}.ogg`);
          try {
            execSync(
              `ffmpeg -y -i ${JSON.stringify(filePath)} -ar 48000 -ac 1 -c:a libopus ${JSON.stringify(tmpPath)}`,
              { timeout: 30000, stdio: 'pipe' }
            );
            audioBuffer = readFileSync(tmpPath);
            audioExt = 'ogg';
          } catch (convErr) {
            // ffmpeg not available or conversion failed — fall back to original format
            console.warn('[bridge] ffmpeg conversion failed, sending as file attachment:', convErr.message);
          } finally {
            try { if (tmpPath && existsSync(tmpPath)) unlinkSync(tmpPath); } catch (_) {}
          }
        }
        const audioMime = (audioExt === 'ogg' || audioExt === 'opus') ? 'audio/ogg; codecs=opus' : 'audio/mpeg';
        msgPayload = { audio: audioBuffer, mimetype: audioMime, ptt: audioExt === 'ogg' || audioExt === 'opus' };
        break;
      }
      case 'document':
      default:
        msgPayload = {
          document: buffer,
          fileName: fileName || path.basename(filePath),
          caption: caption || undefined,
          mimetype: MIME_MAP[ext] || 'application/octet-stream',
        };
        break;
    }

    const sent = await sendWithTimeout(chatId, msgPayload);

    trackSentMessageId(sent);

    res.json({ success: true, messageId: sent?.key?.id });
  } catch (err) {
    res.status(500).json({ error: err.message });
  }
});

// Typing indicator
app.post('/typing', async (req, res) => {
  if (!sock || connectionState !== 'connected') {
    return res.status(503).json({ error: 'Not connected' });
  }

  const { chatId } = req.body;
  if (!chatId) return res.status(400).json({ error: 'chatId required' });

  try {
    await sock.sendPresenceUpdate('composing', chatId);
    res.json({ success: true });
  } catch (err) {
    res.json({ success: false });
  }
});

// Chat info
app.get('/chat/:id', async (req, res) => {
  const chatId = req.params.id;
  const isGroup = chatId.endsWith('@g.us');

  if (isGroup && sock) {
    try {
      const metadata = await sock.groupMetadata(chatId);
      return res.json({
        name: metadata.subject,
        isGroup: true,
        participants: metadata.participants.map(p => p.id),
      });
    } catch {
      // Fall through to default
    }
  }

  res.json({
    name: chatId.replace(/@.*/, ''),
    isGroup,
    participants: [],
  });
});

// Health check
app.get('/health', (req, res) => {
  res.json({
    status: connectionState,
    queueLength: messageQueue.length,
    uptime: process.uptime(),
    scriptHash: SCRIPT_HASH,
    pairing: {
      active: !!(pairingPhone || pairingQrDataUrl),
      mode: PAIRING_MODE,
      phone: pairingPhone,
      code: pairingCode,
      error: pairingError,
      paired: !!pairedAt,
      has_qr: !!pairingQrDataUrl,
    },
  });
});

// Pairing QR snapshot (PNG data URL). Returns the latest QR rendered
// from the Baileys `qr` event. 404 if the bridge is in 'phone' mode or
// no QR has been emitted yet.
app.get('/pairing-qr', (req, res) => {
  if (PAIRING_MODE !== 'qr') {
    return res.status(400).json({ error: 'Bridge is in phone-pairing mode' });
  }
  if (!pairingQrDataUrl) {
    return res.status(404).json({ error: 'No QR available yet' });
  }
  res.json({
    mode: PAIRING_MODE,
    qr: pairingQrDataUrl,
    paired: !!pairedAt,
  });
});

// Pairing code snapshot (explicit endpoint for the dashboard).
// Returns the current code if a pairing flow is in progress; 404 if not.
app.get('/pairing-code', (req, res) => {
  if (!pairingPhone) {
    return res.status(404).json({ error: 'No pairing in progress' });
  }
  res.json({
    phone: pairingPhone,
    code: pairingCode,
    error: pairingError,
    paired: !!pairedAt,
  });
});

// Start
if (PAIR_ONLY) {
  // Pair-only mode: keep the bridge alive long enough for the dashboard
  // to poll /health + /pairing-code. The HTTP server runs on the same
  // port as the full bridge — there should only ever be one instance
  // (the dashboard enforces a single pairing at a time). We deliberately
  // do NOT start the full message queue / /send / /messages handlers
  // beyond the endpoints required for the pairing state machine.
  console.log('📱 WhatsApp pairing mode');
  console.log(`📁 Session: ${SESSION_DIR}`);
  console.log();
  app.listen(PORT, '127.0.0.1', () => {
    console.log(`🌉 WhatsApp pairing bridge listening on port ${PORT}`);
    startSocket();
  });
} else {
  app.listen(PORT, '127.0.0.1', () => {
    console.log(`🌉 WhatsApp bridge listening on port ${PORT} (mode: ${WHATSAPP_MODE})`);
    console.log(`📁 Session stored in: ${SESSION_DIR}`);
    if (ALLOWED_USERS.size > 0) {
      console.log(`🔒 Allowed users: ${Array.from(ALLOWED_USERS).join(', ')}`);
    } else if (WHATSAPP_MODE === 'self-chat') {
      console.log(`🔒 Self-chat mode — only your own messages to yourself are processed.`);
    } else {
      console.log(`🔒 No WHATSAPP_ALLOWED_USERS set — incoming messages are rejected.`);
      console.log(`   Set WHATSAPP_ALLOWED_USERS=<phone> to authorize specific users,`);
      console.log(`   or WHATSAPP_ALLOWED_USERS=* for an explicit open bot.`);
    }
    console.log();
    startSocket();
  });
}
