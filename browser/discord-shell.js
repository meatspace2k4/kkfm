// Discord Activity shell for the KKFM aircheck stream.
//
// This is the whole Discord integration layer: initialize the Embedded App
// SDK, run the OAuth handshake (the server exchanges the code in StreamServer's
// /api/auth/token), then get out of the way so index.html's existing PCM audio
// client streams /stream unchanged. Running inside a voice-channel Activity
// bypasses Discord's Opus voice-gateway bitrate cap.
//
// The SDK is dynamically imported only when we're actually inside Discord, so a
// normal browser tab never depends on the CDN and the aircheck page keeps
// working offline. The bare "@discord/embedded-app-sdk" specifier is resolved
// by the import map in index.html; for production inside Discord, vendor the SDK
// to the same origin and repoint that map (Discord's CSP proxies external
// origins through /.proxy/, so a same-origin file is the reliable option).

// Discord injects launch params (frame_id, instance_id, ...) on the Activity
// URL. Their absence means we're just a normal browser tab, so skip the SDK.
export function isInsideDiscord() {
  return new URLSearchParams(location.search).has('frame_id');
}

let sdkPromise = null;

// Runs the handshake at most once; returns the ready, authenticated DiscordSDK.
export function initDiscordShell() {
  if (sdkPromise) return sdkPromise;
  sdkPromise = handshake().catch((err) => {
    sdkPromise = null; // allow a retry after a failure
    throw err;
  });
  return sdkPromise;
}

async function handshake() {
  const params = new URLSearchParams(location.search);
  const clientId = await resolveClientId(params);
  if (!clientId) {
    throw new Error('Discord client_id not configured (set ACNH_DISCORD_CLIENT_ID)');
  }

  const { DiscordSDK } = await import('@discord/embedded-app-sdk');
  const sdk = new DiscordSDK(clientId);
  await sdk.ready();

  // OAuth authorize -> code. Discord requires this even though the audio path
  // never needs user identity; "identify" is the minimal scope.
  const { code } = await sdk.commands.authorize({
    client_id: clientId,
    response_type: 'code',
    state: '',
    prompt: 'none',
    scope: ['identify'],
  });

  // Exchange the code for an access token via StreamServer (C++).
  const tokenResp = await fetch('/discord-token', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ code }),
  });
  if (!tokenResp.ok) {
    throw new Error('Token exchange failed (HTTP ' + tokenResp.status + ')');
  }
  const { access_token } = await tokenResp.json();
  if (!access_token) throw new Error('Server returned no access_token');

  // Completes the handshake; the Activity is now authenticated.
  await sdk.commands.authenticate({ access_token });
  return sdk;
}

// Prefer the client_id Discord may pass on the launch URL, else ask the server
// (which reads ACNH_DISCORD_CLIENT_ID), so the id lives in config, not source.
async function resolveClientId(params) {
  const fromUrl = params.get('client_id');
  if (fromUrl) return fromUrl;
  try {
    const r = await fetch('/discord-config');
    if (r.ok) {
      const j = await r.json();
      return j.client_id || '';
    }
  } catch (_) {
    // fall through
  }
  return '';
}
