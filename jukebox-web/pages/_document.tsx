import { Html, Head, Main, NextScript } from 'next/document'

export default function Document() {
  return (
    <Html lang="en">
      <Head>
        <link rel="stylesheet" href="https://use.typekit.net/cpb3eoy.css"></link>
        {/* Resolves lib/discordShell.ts's "@discord/embedded-app-sdk" import to a
            same-origin vendored copy (public/vendor/embedded-app-sdk.js), same
            pattern as the radio's browser/index.html -- Discord's CSP proxies
            external origins through /.proxy/, so a same-origin file is what
            actually works once this runs inside the Activity iframe. */}
        <script
          type="importmap"
          dangerouslySetInnerHTML={{
            __html: JSON.stringify({
              imports: { '@discord/embedded-app-sdk': '/vendor/embedded-app-sdk.js' },
            }),
          }}
        />
      </Head>
      <body>
        <Main />
        <NextScript />
      </body>
    </Html>
  )
}