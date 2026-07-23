const fs = require('node:fs')
const os = require('node:os')
const path = require('node:path')
const { spawn } = require('node:child_process')
const {
  app,
  BrowserWindow,
  dialog,
  Menu,
  nativeImage,
  net,
  shell,
  Tray,
} = require('electron')
const semver = require('semver')

const APP_DATA_DIR_NAME = 'TraceLog'
const ENGINE_PORT_PATTERN = /^TRACELOG_PORT=(\d+)$/
const ENGINE_START_TIMEOUT_MS = 30_000
const ENGINE_STOP_TIMEOUT_MS = 8_000
const GITHUB_API_LATEST = 'https://api.github.com/repos/sld272/TraceLog/releases/latest'
const GITHUB_RELEASES = 'https://github.com/sld272/TraceLog/releases'
const desktopSmoke = process.env.TRACELOG_DESKTOP_SMOKE === '1'
const desktopSmokeMarker = process.env.TRACELOG_DESKTOP_SMOKE_MARKER

let engineProcess = null
let engineUrl = null
let mainWindow = null
let tray = null
let quitting = false
let shutdownPromise = null

const dataDir = path.join(app.getPath('appData'), APP_DATA_DIR_NAME)
const engineDataDir = process.env.TRACELOG_DATA_DIR
  ? resolveDataDir(process.env.TRACELOG_DATA_DIR)
  : dataDir
app.setPath('userData', dataDir)

const hasSingleInstanceLock = desktopSmoke || app.requestSingleInstanceLock()
if (!hasSingleInstanceLock) {
  app.quit()
} else {
  registerApplicationEvents()
}

function registerApplicationEvents() {
  app.on('second-instance', showMainWindow)

  app.on('before-quit', (event) => {
    if (quitting || !engineProcess) {
      return
    }
    event.preventDefault()
    quitting = true
    shutdownEngine().finally(() => app.exit(0))
  })

  app.on('activate', showMainWindow)
  app.on('window-all-closed', () => {})

  process.once('exit', () => killEngineProcessGroup())
  process.once('uncaughtException', handleFatalShellError)
  process.once('unhandledRejection', handleFatalShellError)

  app.whenReady().then(startDesktop).catch((error) => {
    // Clicking Quit before the engine prints its port rejects startDesktop after shutdown.
    if (!quitting) {
      handleFatalShellError(error)
    }
  })
}

async function startDesktop() {
  if (!['darwin', 'win32'].includes(process.platform)) {
    throw new Error(`Unsupported desktop platform: ${process.platform}`)
  }
  fs.mkdirSync(dataDir, { recursive: true, mode: 0o700 })
  if (process.platform !== 'win32') {
    fs.chmodSync(dataDir, 0o700)
  }
  fs.mkdirSync(engineDataDir, { recursive: true, mode: 0o700 })
  installApplicationMenu()
  createTray()
  engineUrl = await startEngine()
  await waitForHealth(engineUrl)
  createMainWindow(engineUrl)
  setTimeout(() => {
    checkForUpdates(false).catch(() => {})
  }, 5_000)
}

function engineExecutablePath() {
  if (process.env.TRACELOG_ENGINE_PATH) {
    return path.resolve(process.env.TRACELOG_ENGINE_PATH)
  }
  const executableName = process.platform === 'win32'
    ? 'tracelog-engine.exe'
    : 'tracelog-engine'
  return path.join(process.resourcesPath, 'engine', executableName)
}

function startEngine() {
  const executable = engineExecutablePath()
  fs.accessSync(executable, fs.constants.X_OK)

  return new Promise((resolve, reject) => {
    let stdoutBuffer = ''
    let settled = false
    const timeout = setTimeout(() => {
      if (!settled) {
        settled = true
        reject(new Error('Timed out waiting for the TraceLog engine port'))
      }
    }, ENGINE_START_TIMEOUT_MS)

    engineProcess = spawn(executable, [], {
      cwd: engineDataDir,
      detached: true,
      env: {
        ...process.env,
        TRACELOG_DATA_DIR: engineDataDir,
        TRACELOG_PARENT_PIPE: '1',
      },
      stdio: ['pipe', 'pipe', 'pipe'],
      windowsHide: true,
    })

    engineProcess.stdout.setEncoding('utf8')
    engineProcess.stdout.on('data', (chunk) => {
      stdoutBuffer += chunk
      const lines = stdoutBuffer.split(/\r?\n/)
      stdoutBuffer = lines.pop() ?? ''
      for (const line of lines) {
        const match = ENGINE_PORT_PATTERN.exec(line.trim())
        if (match && !settled) {
          settled = true
          clearTimeout(timeout)
          resolve(`http://127.0.0.1:${match[1]}`)
        }
      }
    })
    engineProcess.stderr.setEncoding('utf8')
    engineProcess.stderr.on('data', (chunk) => {
      process.stderr.write(`[TraceLog engine] ${chunk}`)
    })
    engineProcess.once('error', (error) => {
      if (!settled) {
        settled = true
        clearTimeout(timeout)
        reject(error)
      }
    })
    engineProcess.once('exit', (code, signal) => {
      const exitedDuringStartup = !settled
      engineProcess = null
      if (exitedDuringStartup) {
        settled = true
        clearTimeout(timeout)
        reject(new Error(`TraceLog engine exited during startup (${signal || code})`))
      } else if (!quitting) {
        dialog.showErrorBox(
          'TraceLog 已停止',
          '后台服务意外退出，请重新打开 TraceLog。',
        )
        app.quit()
      }
    })
  })
}

async function waitForHealth(baseUrl) {
  const deadline = Date.now() + ENGINE_START_TIMEOUT_MS
  while (Date.now() < deadline) {
    try {
      const response = await net.fetch(`${baseUrl}/api/health`)
      if (response.ok) {
        return
      }
    } catch {
      // The engine has printed its port but uvicorn has not started accepting requests yet.
    }
    await delay(200)
  }
  throw new Error('Timed out waiting for the TraceLog engine health check')
}

function createMainWindow(baseUrl) {
  mainWindow = new BrowserWindow({
    width: 1280,
    height: 840,
    minWidth: 960,
    minHeight: 640,
    show: false,
    title: 'TraceLog 拾迹',
    webPreferences: {
      contextIsolation: true,
      devTools: false,
      nodeIntegration: false,
      sandbox: true,
    },
  })

  mainWindow.once('ready-to-show', () => mainWindow.show())
  mainWindow.on('close', (event) => {
    if (!quitting) {
      event.preventDefault()
      mainWindow.hide()
    }
  })
  mainWindow.on('closed', () => {
    mainWindow = null
  })
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (isSafeExternalUrl(url)) {
      shell.openExternal(url)
    }
    return { action: 'deny' }
  })
  mainWindow.webContents.on('will-navigate', (event, url) => {
    if (!url.startsWith(`${baseUrl}/`)) {
      event.preventDefault()
      if (isSafeExternalUrl(url)) {
        shell.openExternal(url)
      }
    }
  })
  if (desktopSmoke) {
    mainWindow.webContents.once('did-finish-load', () => {
      if (desktopSmokeMarker) {
        fs.writeFileSync(desktopSmokeMarker, 'TRACELOG_DESKTOP_SMOKE_OK\n')
      }
      app.quit()
    })
  }
  mainWindow.loadURL(`${baseUrl}/`)
}

function createTray() {
  const trayPath = path.join(process.resourcesPath, 'assets', 'tray.png')
  const trayImage = nativeImage.createFromPath(trayPath).resize({ width: 18, height: 18 })
  tray = new Tray(trayImage)
  tray.setToolTip('TraceLog 拾迹')
  tray.on('click', showMainWindow)
  rebuildTrayMenu()
}

function rebuildTrayMenu() {
  const openAtLogin = app.getLoginItemSettings().openAtLogin
  tray.setContextMenu(Menu.buildFromTemplate([
    {
      label: '打开 TraceLog',
      click: showMainWindow,
    },
    {
      label: '检查更新…',
      click: () => checkForUpdates(true),
    },
    { type: 'separator' },
    {
      label: '开机自动启动',
      type: 'checkbox',
      checked: openAtLogin,
      click: (item) => {
        app.setLoginItemSettings({ openAtLogin: item.checked })
        rebuildTrayMenu()
      },
    },
    { type: 'separator' },
    {
      label: '退出 TraceLog',
      click: () => app.quit(),
    },
  ]))
}

function installApplicationMenu() {
  Menu.setApplicationMenu(Menu.buildFromTemplate([
    {
      label: app.name,
      submenu: [
        { role: 'about', label: '关于 TraceLog' },
        { type: 'separator' },
        { role: 'hide', label: '隐藏 TraceLog' },
        { role: 'hideOthers', label: '隐藏其他应用' },
        { role: 'unhide', label: '全部显示' },
        { type: 'separator' },
        { label: '退出 TraceLog', accelerator: 'CmdOrCtrl+Q', click: () => app.quit() },
      ],
    },
    {
      label: '编辑',
      submenu: [
        { role: 'undo', label: '撤销' },
        { role: 'redo', label: '重做' },
        { type: 'separator' },
        { role: 'cut', label: '剪切' },
        { role: 'copy', label: '复制' },
        { role: 'paste', label: '粘贴' },
        { role: 'selectAll', label: '全选' },
      ],
    },
    {
      label: '窗口',
      submenu: [
        { role: 'minimize', label: '最小化' },
        { role: 'zoom', label: '缩放' },
        { type: 'separator' },
        { label: '打开 TraceLog', click: showMainWindow },
      ],
    },
  ]))
}

async function checkForUpdates(manual) {
  try {
    const response = await net.fetch(GITHUB_API_LATEST, {
      headers: {
        Accept: 'application/vnd.github+json',
        'User-Agent': `TraceLog/${app.getVersion()}`,
      },
    })
    if (!response.ok) {
      throw new Error(`GitHub release check returned ${response.status}`)
    }
    const release = await response.json()
    const latestVersion = semver.coerce(release.tag_name)
    const currentVersion = semver.coerce(app.getVersion())
    if (latestVersion && currentVersion && semver.gt(latestVersion, currentVersion)) {
      const result = await showMessageBox({
        type: 'info',
        title: 'TraceLog 有新版本',
        message: `TraceLog ${latestVersion.version} 已发布`,
        detail: '当前版本暂不支持应用内更新，可以前往发布页下载新版。',
        buttons: ['前往下载', '稍后'],
        defaultId: 0,
        cancelId: 1,
      })
      if (result.response === 0) {
        await shell.openExternal(release.html_url || GITHUB_RELEASES)
      }
    } else if (manual) {
      await showMessageBox({
        type: 'info',
        title: '检查更新',
        message: '你正在使用最新版 TraceLog。',
        buttons: ['好'],
      })
    }
  } catch (error) {
    if (manual) {
      await showMessageBox({
        type: 'warning',
        title: '暂时无法检查更新',
        message: '请稍后重试，或直接前往 TraceLog 发布页查看。',
        buttons: ['前往发布页', '取消'],
        defaultId: 0,
        cancelId: 1,
      }).then((result) => {
        if (result.response === 0) {
          return shell.openExternal(GITHUB_RELEASES)
        }
        return undefined
      })
    }
    process.stderr.write(`TraceLog update check failed: ${String(error)}\n`)
  }
}

function showMainWindow() {
  if (!mainWindow) {
    if (engineUrl && app.isReady()) {
      createMainWindow(engineUrl)
    }
    return
  }
  if (mainWindow.isMinimized()) {
    mainWindow.restore()
  }
  mainWindow.show()
  mainWindow.focus()
}

function showMessageBox(options) {
  if (mainWindow) {
    return dialog.showMessageBox(mainWindow, options)
  }
  return dialog.showMessageBox(options)
}

function shutdownEngine() {
  if (shutdownPromise) {
    return shutdownPromise
  }
  const child = engineProcess
  if (!child) {
    return Promise.resolve()
  }
  shutdownPromise = new Promise((resolve) => {
    const timeout = setTimeout(() => {
      killEngineProcessGroup('SIGKILL')
      resolve()
    }, ENGINE_STOP_TIMEOUT_MS)
    child.once('exit', () => {
      clearTimeout(timeout)
      resolve()
    })
    child.stdin.end()
  })
  return shutdownPromise
}

function killEngineProcessGroup(signal = 'SIGTERM') {
  if (!engineProcess || !engineProcess.pid) {
    return
  }
  try {
    if (process.platform === 'win32') {
      engineProcess.kill(signal)
    } else {
      process.kill(-engineProcess.pid, signal)
    }
  } catch (error) {
    if (error.code !== 'ESRCH') {
      process.stderr.write(`Failed to stop TraceLog engine: ${String(error)}\n`)
    }
  }
}

function handleFatalShellError(error) {
  process.stderr.write(`TraceLog shell failed: ${String(error)}\n`)
  if (app.isReady()) {
    dialog.showErrorBox(
      'TraceLog 无法启动',
      '后台服务没有成功启动，请退出后重试。',
    )
  }
  quitting = true
  killEngineProcessGroup()
  app.exit(1)
}

function isSafeExternalUrl(url) {
  try {
    return new URL(url).protocol === 'https:'
  } catch {
    return false
  }
}

function resolveDataDir(value) {
  if (value === '~') {
    return os.homedir()
  }
  if (value.startsWith('~/')) {
    return path.join(os.homedir(), value.slice(2))
  }
  return path.resolve(value)
}

function delay(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds))
}
