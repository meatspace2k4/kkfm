// Client side of the multicast broadcast: fetch the server's authoritative
// {songId, startedAtMs} for this Discord Activity instance and compute how
// far into that song "now" actually is, so a newly-joining (or resyncing)
// client starts mid-buffer at roughly the right spot instead of at 0 -- like
// tuning into a station already playing, not restarting it for each listener.
//
// Doesn't need millisecond precision (each song re-anchors this every time it
// changes, so drift can't accumulate across a whole session): a simple
// round-trip-time correction on the clock offset is enough.

export interface NowPlaying {
  songId: number;
  startedAtMs: number;
  serverNowMs: number;
  durationMs: number;
}

export interface BroadcastPosition {
  songId: number;
  offsetSec: number;
  durationSec: number;
}

// Fetches /now-playing and returns the current song + how far into it we are,
// correcting for network latency and any client/server clock offset by timing
// the request round-trip and assuming roughly symmetric latency.
//
// Requires a real Discord instanceId. There's no anonymous/no-instance
// fallback session on the server (removed after one caused a real bug: a
// plain browser tab tuned into an unrelated curl-test session mid-song), so
// this throws immediately rather than sending a request that would just
// come back 400 -- fail at the call site, not after a round trip.
export async function fetchBroadcastPosition(instanceId: string | null): Promise<BroadcastPosition> {
  if (!instanceId) {
    throw new Error('fetchBroadcastPosition called without a Discord instanceId -- no shared session to sync to');
  }
  const t0 = Date.now();
  const resp = await fetch(`/now-playing?instance=${encodeURIComponent(instanceId)}`, { cache: 'no-store' });
  const t1 = Date.now();
  if (!resp.ok) throw new Error(`/now-playing -> ${resp.status}`);
  const data: NowPlaying = await resp.json();

  const rttMs = t1 - t0;
  // serverNowMs was stamped when the server built the response, i.e.
  // approximately t0 + (one-way trip). Estimate client-clock-equivalent "now"
  // as serverNowMs plus the other half of the round trip.
  const estimatedNowMs = data.serverNowMs + rttMs / 2;

  const durationSec = Math.max(0.1, data.durationMs / 1000);
  let offsetSec = (estimatedNowMs - data.startedAtMs) / 1000;
  // Clamp defensively: a slow/odd request shouldn't hand back a negative or
  // past-the-end offset (the player would either seek backwards oddly or
  // immediately hit "ended" on a buffer that hasn't even started).
  offsetSec = Math.min(Math.max(offsetSec, 0), durationSec - 0.05);

  return { songId: data.songId, offsetSec, durationSec };
}
