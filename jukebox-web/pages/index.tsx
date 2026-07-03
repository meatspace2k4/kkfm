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
   * Multicast sync is a CONTROL LAYER on top of the base jukebox, not a
   * replacement for it. The base -- pick the next song from the local shuffle
   * pool, play it with the engine's loop/fade -- always runs. When the client
   * is in a Discord Activity room (a shared instanceId, see lib/discordShell),
   * the server (jukebox_pi's /now-playing) is authoritative about what that
   * room is playing, and it OVERRIDES the base pick at each decision point:
   * the client plays the server's song at the server's offset instead of its
   * own local pick.
   *
   * The override is a short circuit, not a fallback. The base is the default
   * that always executes; the room, when present and answering, preempts it.
   * When there is no room (a plain browser tab: instanceId null), or when
   * we're in a room but the server doesn't answer (logged loudly), nothing
   * preempts and the base simply proceeds -- keeping the music going. The
   * next decision point re-attempts the override, so a room that comes back
   * (rejoining after sleep, or a hypothetical future room change) transparently
   * takes back over at the next song boundary without any special resume path.
   */
  const instanceIdRef = useRef<string | null>(null);

  const scrollToSong = (song: Song) => {
    const el = document.getElementById(makeIdFromSongName(song.name['name-USen']));
    el?.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }

  // The override. Returns true only if it actually took over playback with the
  // room's current song. Never call without being in a room -- "in a room" is
  // a real precondition, checked by the caller, not an error to swallow here.
  const applyRoomOverride = async (): Promise<boolean> => {
    try {
      const pos = await fetchBroadcastPosition(instanceIdRef.current);
      const song = songs.find((s) => s.id === pos.songId);
      if (!song) {
        // In a room, but the server named a song this build doesn't ship.
        // Surface it; the base pick keeps playing.
        console.error(`[broadcast] server song id ${pos.songId} not in this client's set`);
        return false;
      }
      setCurrentSong(song);
      scrollToSong(song);
      await getBwavPlayer().play(song.music_uri, { offsetSec: pos.offsetSec });
      return true;
    } catch (err) {
      // In a room, but the server didn't answer. Do not invent a quiet
      // substitute -- log it and let the base keep the music going; the next
      // boundary re-asserts the room.
      console.error('[broadcast] room query failed, staying on local pool for now:', err);
      return false;
    }
  }

  // The decision made at every song boundary (and reused on load). Base:
  // next song from the local pool. The room, if we're in one, short-circuits it.
  const advanceToNext = async () => {
    if (instanceIdRef.current && (await applyRoomOverride())) return;
    playRandomSong();
  }

  useEffect(() => {
    let cancelled = false;
    (async () => {
      instanceIdRef.current = await getDiscordInstanceId();
      // On load, only the room can start playback on its own: joining an
      // Activity room means tuning into whatever it's already playing, at the
      // right offset. With no room there's nothing to auto-start (and browsers
      // block autoplay without a gesture anyway), so the page stays idle until
      // the user hits Random or a tile -- exactly the original site's behavior.
      if (!cancelled && instanceIdRef.current) applyRoomOverride();
    })();
    return () => { cancelled = true; };
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
    // An explicit user pick (tile click, or Random) plays immediately from the
    // start -- a momentary override of whatever the room is doing, for this
    // client only. No sync bookkeeping needed: when this song ends, the normal
    // boundary decision (advanceToNext) runs and the room, if any, takes back
    // over. So a manual pick simply lasts one song.
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
   * When a song finishes naturally (loop played twice, plays on, then fades to
   * silence — see lib/bwavPlayer), make the boundary decision: advanceToNext()
   * plays the next song from the local pool, unless we're in a room and the
   * server overrides that with what the room is playing. Guarded by the
   * player's own token check, so this never fires for a song the user
   * paused/skipped away from mid-play.
   */
  useEffect(() => {
    const onEnded = () => { advanceToNext(); };
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
