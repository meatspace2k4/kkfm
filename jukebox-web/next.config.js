const useOptimizedClassnames = false;

const defaultConfig = {
  swcMinify: true,
  // Static export: the site is served by the kkfm jukebox C++ server (no Node
  // at runtime). i18n was removed (unsupported with `output: 'export'`; the
  // app only registered 'en' anyway) and next/image optimization is disabled
  // (jackets are already 384px PNGs served same-origin from /jackets).
  output: 'export',
  images: {
    unoptimized: true,
    deviceSizes: [640, 750, 828, 1080, 1200, 1920],
    imageSizes: [16, 32, 48, 64, 96, 128, 256],
  },
};

module.exports = useOptimizedClassnames
  ? require('./packages/next-optimized-classnames')(defaultConfig)
  : defaultConfig;
