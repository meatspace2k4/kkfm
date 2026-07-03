// Library
import React, { useState, useRef, useEffect, useMemo } from 'react';
import Head from 'next/head';
import MUSIC_DATA from '@/data/music.json';

// Components
import ActionButton from '@/components/ActionButton';
import ActionButtonsMenu from '@/components/ActionButtonsMenu';
import BackgroundCover from '@/components/BackgroundCover';
import Footer from '@/components/Footer';
import GradientWave from '@/components/GradientWave';
import Player from '@/components/Player';
import SearchInput from '@/components/SearchInput';
import SongArt from '@/components/SongArt';
import TouchPrompt from '@/components/TouchPrompt';

// Other
import styles from '@/styles/Home.module.scss';
import { useIsTouchDevice, useKeydown, useLocalStorage } from '@/lib/hooks';
import { makeIdFromSongName, shuffleInPlace } from '@/lib/utils';
import { getBwavPlayer } from '@/lib/bwavPlayer';
import { getDiscordInstanceId } from '@/lib/discordShell';
import { fetchBroadcastPosition } from '@/lib/broadcastSync';

/**
 * The main app component
 */
export default function Home({ songs }: { songs: Song[] }) {
  const [requiresTouchPrompt, setRequiresTouchPrompt] = useState(false);
  const [storage] = useLocalStorage();
  const [isTouchDevice] = useIsTouchDevice();

  // Playback
  const [currentSong, setCurrentSong] = useState<null | Song>(null);
  const audioRef = useRef<HTMLMediaElement>(null);
  const playRandomRef = useRef<() => void>(null); // Stores 'play random' fn

  // Search
  const searchModeRef = useRef<() => void>(null); // Stores 'handle search mode' fn
  const [isSearching, setIsSearching] = useState(false);
  const [searchInput, setSearchInput] = useState('');

  /**
   * Maps key presses to actions and handles executing them.
   * Provides state var and setter for when key presses will work.
   */
  const { setAcceptKeydown } = useKeydown(
    useMemo(() => ({
      'r': playRandomRef,
      's': searchModeRef
    }), [playRandomRef, searchModeRef])
  )

  //
  // UseEffects
  //

  /**
   * Handles showing mobile prompt
   * only once to non-desktop users
   */
  useEffect(() => {
    if (storage) {
      const hasReceivedTouchPromp = storage.getItem('receivedTouchPrompt') === 'true';
      if (isTouchDevice && !hasReceivedTouchPromp) {
        setRequiresTouchPrompt(true);
        storage.setItem('receivedTouchPrompt', 'true');
      }
    }
  }, [isTouchDevice, requiresTouchPrompt, storage])

  // Looping/fade-out is baked into the decoded PCM by the BWAV player (it
  // mirrors the engine's own decoder: loop body plays twice, then fades),
  // so the original client-side loop-back timer (which polled an <audio>
  // element's currentTime) is gone.

  /**
   * Shuffle-bag pool for the Random button and auto-advance: pressing Random
   * shuffles the remaining pool and draws the top song, removing it so it
   * can't repeat until every other song has been played. Once the pool is
   * empty, it's rehydrated with the full song list for the next draw.
   */
  const poolRef = useRef<number[]>(songs.map((_, i) => i));

  const drawNextSong = (): Song => {
    if (poolRef.current.length === 0) poolRef.current = songs.map((_, i) => i);
    shuffleInPlace(poolRef.current);
    const idx = poolRef.current.pop() as number;
    if (poolRef.current.length === 0) poolRef.current = songs.map((_, i) => i);
    return songs[idx];
  }

  /**
   * Multicast broadcast sync: everyone in the same Discord Activity session
   * (same voice channel -> same instanceId, see lib/discordShell) hears the
   * same song at the same position, like tuning into a station. The server
   * (jukebox_pi's /now-playing) is the single authority on "what song, since
   * when" per instance; this client just asks it and starts mid-buffer at the
   * reported offset -- AudioBufferSourceNode.start(when, offset) seeks into
   * the already-fully-decoded buffer natively, no extra plumbing needed.
   *
   * Outside Discord there is no shared instanceId -- no "room" to sync with --
   * so sync must never engage there. instanceIdRef stays null in that case,
   * and every call site below checks it first; a plain browser tab keeps the
   * original solo/local-pool behavior (nothing plays until the user picks
   * something) instead of tuning into whatever the server's anonymous
   * fallback session happens to be playing mid-song.
   *
   * `synced` tracks whether the CURRENT playback is following the broadcast
   * (only possible with a real instanceId) or is a solo listen (a tile click
   * or Random, which sets it false, or simply not being inside Discord at
   * all). A solo pick's natural end returns to the broadcast if one's
   * available, so inside Discord "everyone hears the same thing" is the
   * steady state; outside it, playback just keeps behaving like a normal
   * jukebox.
   */
  const syncedRef = useRef(false);
  const instanceIdRef = useRef<string | null>(null);
  const resyncTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const joinBroadcast = async () => {
    if (!instanceIdRef.current) return; // no shared room to sync with -- stay solo
    if (resyncTimerRef.current) { clearTimeout(resyncTimerRef.current); resyncTimerRef.current = null; }
    try {
      const pos = await fetchBroadcastPosition(instanceIdRef.current);
      const song = songs.find((s) => s.id === pos.songId);
      if (!song) return; // server knows a song this client's build doesn't ship; skip silently

      syncedRef.current = true;
      setCurrentSong(song);
      const el = document.getElementById(makeIdFromSongName(song.name['name-USen']));
      el?.scrollIntoView({ behavior: 'smooth', block: 'center' });

      await getBwavPlayer().play(song.music_uri, { offsetSec: pos.offsetSec });

      // Belt-and-suspenders resync timer for whatever's left of this song, in
      // case the 'bwav-ended' event is ever missed (tab throttling, etc.) --
      // the ended-handler below normally fires first and clears this.
      const remainingMs = Math.max(1000, (pos.durationSec - pos.offsetSec) * 1000 + 500);
      resyncTimerRef.current = setTimeout(() => {
        if (syncedRef.current) joinBroadcast();
      }, remainingMs);
    } catch (err) {
      console.error('[broadcast] join failed:', err);
      // Retry shortly rather than leaving the room permanently out of sync.
      resyncTimerRef.current = setTimeout(() => joinBroadcast(), 5000);
    }
  }

  useEffect(() => {
    let cancelled = false;
    (async () => {
      instanceIdRef.current = await getDiscordInstanceId();
      // Only auto-join when there's an actual shared room; otherwise leave the
      // page idle (matches the original site: nothing plays until the user
      // clicks Random or a tile).
      if (!cancelled && instanceIdRef.current) joinBroadcast();
    })();
    return () => {
      cancelled = true;
      if (resyncTimerRef.current) clearTimeout(resyncTimerRef.current);
    };
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  //
  // Component Functions
  //

  /**
   * Playback is handled by the BWAV Web Audio player (lib/bwavPlayer), not an
   * <audio> element: the server sends raw ADPCM which the browser can't decode
   * natively, so we decode it with the kkfm engine's algorithm and play it
   * through Web Audio (gapless native looping, so no client loop timer).
   * setAudioUri stashes the pending url; handlePlay starts it (from a click,
   * satisfying the AudioContext autoplay gesture requirement).
   */
  const pendingUriRef = useRef<string>('');

  const handlePlay = () => {
    const uri = pendingUriRef.current;
    if (!uri) return;
    // Any explicit single-song play (tile click, or Random via playRandomSong
    // below) is a deliberate solo listen, not the shared broadcast -- stop
    // following it, and cancel any pending resync so it can't cut this off
    // mid-play. The 'bwav-ended' handler returns to the broadcast once this
    // song naturally finishes.
    syncedRef.current = false;
    if (resyncTimerRef.current) { clearTimeout(resyncTimerRef.current); resyncTimerRef.current = null; }
    getBwavPlayer().play(uri).catch((err: unknown) => console.error('[bwav] play failed:', err));
  }

  const handlePause = () => {
    getBwavPlayer().pause();
  }

  const setAudioUri = (uri: string) => {
    if (uri) pendingUriRef.current = uri;
  }

  /**
   * Used by serveral components to enter or
   * exit search mode. 
   * - BackgroundCover
   * - Search Action button
   * - Mapped to 's' key for keydown events
   */
  const toggleSearchMode = () => {
    if (isSearching) {
      setIsSearching(false);
      setAcceptKeydown(true);
      setSearchInput('');
    } else {
      setIsSearching(true);
      setAcceptKeydown(false);
    }
  }
  // @ts-ignore - Cannot assign to 'current' because it is a read-only property
  searchModeRef.current = toggleSearchMode; // Manually set ref after fn creation

  /**
   * Short hand to cancel out of search mode only
   * if we're currently in it.
   */
  const cancelSearchMode = () => {
    if (isSearching) {
      toggleSearchMode();
    }
  }

  /**
   * Used by RANDOM button and gets mapped to 'r' key
   * using a Ref object in useKeydown(). Also reused by the auto-advance
   * effect below, so "ended" behaves like a continuous radio: it draws the
   * next song from the same shuffle-bag pool.
   */
  const playRandomSong = () => {
    // Cancel search if clicked while in search mode.
    cancelSearchMode();

    const song = drawNextSong();

    setCurrentSong(song);
    setAudioUri(song.music_uri);
    handlePlay();

    const id = makeIdFromSongName(song.name['name-USen']);
    const el = document.getElementById(id);
    el?.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }
  // @ts-ignore - Cannot assign to 'current' because it is a read-only property
  playRandomRef.current = playRandomSong; // Manually set ref after fn creation

  /**
   * When a song finishes naturally (loop played twice, plays on, then fades
   * to silence — see lib/bwavPlayer), advance. Inside Discord (a real
   * instanceId) this rejoins the shared broadcast -- true whether the song
   * that just ended was itself the broadcast or a solo pick (Random / a tile
   * click), so "everyone hears the same thing" is the steady state. Outside
   * Discord there's no room to rejoin, so it draws another song from the
   * local shuffle-bag pool instead, same as the original site. Guarded by the
   * player's own token check, so this never fires for a song the user
   * paused/skipped away from mid-play.
   */
  useEffect(() => {
    const onEnded = () => {
      if (instanceIdRef.current) joinBroadcast();
      else playRandomSong();
    };
    window.addEventListener('bwav-ended', onEnded);
    return () => window.removeEventListener('bwav-ended', onEnded);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  /**
   * Used to filter songs when searching
   */
  const filterSongsFromSearch = (song: Song) => {
    if (!isSearching) return true;
    const name = song.name['name-USen'].toLowerCase();
    const input = searchInput.toLowerCase();
    return name.includes(input);
  }

  return (
    <div className={styles.app}>
      <Head>
        <title>ACNH Jukebox - Animal Crossing: New Horizons Music Player</title>
        
        <meta name="viewport" content="initial-scale=1.0, width=device-width" />
        <meta charSet="utf-8" />
        <meta name="description" content="A Web app for playing K.K. Slider jams like in Animal Crossing: New Horizons." />
        <meta name="keywords" content='kk slider, music player, game music'/>
        <meta name="og:title" content='ACNH Jukebox'/>
        <meta name="og:url" content='https://acnhjukebox.vercel.app'/>
        <meta name="og:description" content='A music player for K.K. Slider songs like in Animal Crossing: New Horizons.'/>
        <meta name="og:image" content='https://acnhapi.com/v1/images/songs/95'/>

        <link rel="icon" href="data:image/svg+xml,<svg xmlns=%22http://www.w3.org/2000/svg%22 viewBox=%220 0 100 100%22><text y=%22.9em%22 font-size=%2290%22>🏝</text></svg>" />
      </Head>

      {isTouchDevice && requiresTouchPrompt && 
      <TouchPrompt 
        setRequiresTouchPrompt={setRequiresTouchPrompt} 
      />}

      <main className={styles.main}>
        {isSearching ? (
          <>
            <SearchInput 
              searchInput={searchInput} 
              setSearchInput={setSearchInput}
              cancelSearchMode={cancelSearchMode}
            />
            <BackgroundCover onClick={toggleSearchMode} />
          </>
        ) : (
          <Player 
            currentSong={currentSong} 
            audioRef={audioRef} 
          />
        )}

        <ActionButtonsMenu>
          <ActionButton symbol="r" name='random' onAction={playRandomSong} />
          <ActionButton symbol="s" name='search' onAction={toggleSearchMode} />
        </ActionButtonsMenu>
        
        <ol className={styles.songList}>
          {songs.filter(filterSongsFromSearch).map((song: Song) => {
            const isCurrentSong = currentSong?.id === song.id;
            return (
              <SongArt
                key={song.id}
                song={song}
                isCurrentSong={isCurrentSong}
                setCurrentSong={setCurrentSong}
                handlePlay={handlePlay}
                handlePause={handlePause}
                setAudioUri={setAudioUri}
                cancelSearchMode={cancelSearchMode}
              />
            );
          })}
        </ol>
      </main>

      <Footer />

      <GradientWave id={1} color='rgba(41,150,253,1)' />
      <GradientWave id={2} color='rgba(255,255,255,0.3)' />
      <GradientWave id={3} color='rgba(102,121,251,1)' />
    </div>
  );
}

export async function getStaticProps() {
  /** ACNH API is offline :'( */
  // const req = await fetch('https://acnhapi.com/v1a/songs');
  // const data: Song[] = await req.json();

  /**
   * Remove unused props from data
   */
  // const prunedSongs = data.map((song: Song) => {
  //   const {
  //     'file-name': _unused_fileName,
  //     'sell-price': _unused_sellPrice,
  //     'buy-price': _unused_buyPrice,
  //     isOrderable: _unused_isOrderable,
  //     ...prunedSongData
  //   } = song;

  //   return prunedSongData;
  // })


  // Assets come from the kkfm jukebox engine on this same origin:
  // music_uri/image_uri in data/music.json point at /song/<binId>.wav
  // (rendered per-request by the C++ engine) and /jackets/<file>.png
  // (album art extracted from the game's record models).
  const prunedSongs: Song[] = []
  for (const song in MUSIC_DATA) {
    //@ts-ignore MUSIC_DATA is implicit any
    const {id, name, music_uri, image_uri} = MUSIC_DATA[song];
    prunedSongs.push({
      id,
      name,
      music_uri,
      image_uri
    })
  }

  return {
    props: {
      songs: prunedSongs
    },
  };
}
