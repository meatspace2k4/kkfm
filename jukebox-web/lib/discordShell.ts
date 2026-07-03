// Discord Activity shell for the jukebox. Same handshake as
// browser/discord-shell.js (the radio's own copy), ported to TS for the
// Next.js app. The resolved DiscordSDK instance's `instanceId` is the key
// piece: per Discord's own docs (docs.discord.com/developers/activities/
// development-guides/multiplayer-experience), everyone connected to the same
// Activity session (same voice channel) receives the same instanceId, and
// it's available immediately after `new DiscordSDK(clientId)` -- no need to
// wait for the OAuth handshake to finish just to read it.
//
// The SDK is dynamically imported only when we're actually inside Discord, so
// a normal browser tab never depends on it. The bare "@discord/embedded-app-sdk"
// specifier is resolved by the import map in pages/_document.tsx, pointing at
// a same-origin vendored copy (public/vendor/embedded-app-sdk.js) -- Discord's
// CSP proxies external origins through /.proxy/, so same-origin is what
// actually works in production.

// Minimal shape of what we use from the SDK; avoids a hard dependency on its
// full type package (not installed -- the module is loaded at runtime only).
export interface DiscordSdkLike {
  instanceId: string;
  guildId?: string | null;
  channelId?: string | null;
  ready(): Promise<void>;
  commands: {
    authorize(opts: {
      client_id: string;
      response_type: string;
      state: string;
      prompt: string;
      scope: string[];
    }): Promise<{ code: string }>;
    authenticate(opts: { access_token: string }): Promise<unknown>;
  };
}

export function isInsideDiscord(): boolean {
  if (typeof location === 'undefined') return false;
  return new URLSearchParams(location.search).has('frame_id');
}

let sdkPromise: Promise<DiscordSdkLike> | null = null;

// Runs the handshake at most once; returns the ready, authenticated SDK.
export function initDiscordShell(): Promise<DiscordSdkLike> {
  if (sdkPromise) return sdkPromise;
  sdkPromise = handshake().catch((err) => {
    sdkPromise = null; // allow a retry after a failure
    throw err;
  });
  return sdkPromise;
}

async function handshake(): Promise<DiscordSdkLike> {
  const params = new URLSearchParams(location.search);
  const clientId = await resolveClientId(params);
  if (!clientId) {
    throw new Error('Discord client_id not configured (set ACNH_DISCORD_CLIENT_ID)');
  }

  // webpackIgnore: leave this import statement untouched in the bundle --
  // it's resolved by the browser's native <script type="importmap"> (see
  // pages/_document.tsx) at runtime, not by webpack at build time. Without
  // it, webpack tries to statically resolve "@discord/embedded-app-sdk" as
  // an installed package and fails (it isn't one -- it's vendored as a
  // same-origin static file instead, same pattern as the radio's page).
  // @ts-expect-error -- resolved via the import map at runtime, not a local package.
  const { DiscordSDK } = await import(/* webpackIgnore: true */ '@discord/embedded-app-sdk');
  const sdk: DiscordSdkLike = new DiscordSDK(clientId);
  await sdk.ready();

  const { code } = await sdk.commands.authorize({
    client_id: clientId,
    response_type: 'code',
    state: '',
    prompt: 'none',
    scope: ['identify'],
  });

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

  await sdk.commands.authenticate({ access_token });
  return sdk;
}

async function resolveClientId(params: URLSearchParams): Promise<string> {
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

// Best-effort instanceId lookup for the broadcast-sync key. Inside Discord
// this waits for the full handshake (matching the radio's own pattern) and
// returns the shared instanceId. Outside Discord there's no shared session to
// key off of, so callers fall back to a single global broadcast (see
// lib/broadcastSync.ts) rather than getting stuck waiting on a promise that
// will never resolve.
export async function getDiscordInstanceId(): Promise<string | null> {
  if (!isInsideDiscord()) return null;
  try {
    const sdk = await initDiscordShell();
    return sdk.instanceId || null;
  } catch (err) {
    console.error('[discord] instanceId lookup failed:', err);
    return null;
  }
}
