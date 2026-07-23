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
    target: ['dmg'],
    category: 'public.app-category.productivity',
    icon: 'build/icon.icns',
    artifactName: 'TraceLog-${version}-macOS-Arm64.${ext}',
    identity: hasSigningIdentity ? undefined : null,
    hardenedRuntime: true,
    gatekeeperAssess: false,
    entitlements: 'entitlements.mac.plist',
    entitlementsInherit: 'entitlements.mac.plist',
    notarize: hasSigningIdentity && (hasAppleIdNotary || hasApiKeyNotary),
  },
  win: {
    target: [
      {
        target: 'nsis',
        arch: ['x64'],
      },
    ],
    icon: 'build/icon.ico',
  },
  nsis: {
    artifactName: 'TraceLog-${version}-Windows-x64-Installer.${ext}',
    oneClick: false,
    allowToChangeInstallationDirectory: true,
    createDesktopShortcut: true,
    createStartMenuShortcut: true,
    shortcutName: 'TraceLog 拾迹',
    uninstallDisplayName: 'TraceLog 拾迹',
  },
  dmg: {
    sign: false,
    title: 'TraceLog 拾迹 ${version}',
  },
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
