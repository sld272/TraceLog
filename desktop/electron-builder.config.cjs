const path = require('node:path')

const hasSigningIdentity = Boolean(process.env.CSC_LINK || process.env.CSC_NAME)
const hasAppleIdNotary = Boolean(
  process.env.APPLE_ID
    && process.env.APPLE_APP_SPECIFIC_PASSWORD
    && process.env.APPLE_TEAM_ID,
)
const hasApiKeyNotary = Boolean(
  process.env.APPLE_API_KEY
    && process.env.APPLE_API_KEY_ID
    && process.env.APPLE_API_ISSUER,
)

module.exports = {
  appId: 'com.sld272.tracelog',
  productName: 'TraceLog 拾迹',
  directories: {
    output: 'dist/shell',
    buildResources: 'build',
  },
  files: [
    'shell/**/*',
    'package.json',
  ],
  extraResources: [
    {
      from: 'dist/engine/tracelog-engine',
      to: 'engine',
    },
    {
      from: '../frontend/public/brand/tracelog-icon-transparent-256.png',
      to: 'assets/tray.png',
    },
  ],
  mac: {
    target: ['dmg', 'zip'],
    category: 'public.app-category.productivity',
    icon: 'build/icon.icns',
    identity: hasSigningIdentity ? undefined : null,
    hardenedRuntime: true,
    gatekeeperAssess: false,
    entitlements: 'entitlements.mac.plist',
    entitlementsInherit: 'entitlements.mac.plist',
    notarize: hasSigningIdentity && (hasAppleIdNotary || hasApiKeyNotary),
  },
  dmg: {
    sign: false,
    title: 'TraceLog 拾迹 ${version}',
  },
  // arm64-only build: name it for humans — these DMGs run only on Apple Silicon (M-series) Macs.
  artifactName: 'TraceLog-${version}-macOS-AppleSilicon.${ext}',
  publish: {
    provider: 'github',
    owner: 'sld272',
    repo: 'TraceLog',
  },
  extraMetadata: {
    main: 'shell/main.cjs',
  },
  electronDownload: {
    cache: path.join(__dirname, 'build', 'electron-cache'),
  },
}
