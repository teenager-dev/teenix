const { app, autoUpdater, BrowserWindow, dialog, ipcMain, shell } = require('electron');
const { spawn } = require('node:child_process');
const http = require('node:http');
const path = require('node:path');

let backend = null;
let mainWindow = null;

function waitForBackend(port, child) {
  return new Promise((resolve, reject) => {
    const deadline = Date.now() + 30000;
    const check = () => {
      if (child.exitCode !== null) return reject(new Error('Il server Python si è chiuso durante l’avvio.'));
      const request = http.get(`http://127.0.0.1:${port}/`, response => {
        response.resume();
        if (response.statusCode === 200) return resolve();
        retry();
      });
      request.setTimeout(1200, () => request.destroy());
      request.on('error', retry);
    };
    const retry = () => {
      if (Date.now() >= deadline) return reject(new Error('Il server locale non risponde. Controlla Python e le dipendenze in requirements.txt.'));
      setTimeout(check, 300);
    };
    check();
  });
}

function startBackend(serverPath, root, port) {
  return new Promise((resolve, reject) => {
    const args = [serverPath, '--host', '127.0.0.1', '--port', String(port)];
    const configured = process.env.TEENIX_PYTHON || process.env.STREAMSCRAP_PYTHON;
    const commands = configured
      ? [[configured, []]]
      : process.platform === 'win32'
        ? [['py', ['-3']], ['python', []]]
        : [['python3', []], ['python', []]];
    const attempt = index => {
      const [command, prefix] = commands[index];
      const child = spawn(command, [...prefix, ...args], {
        cwd: root,
        windowsHide: true,
        stdio: 'ignore',
      });
      child.once('spawn', () => resolve(child));
      child.once('error', error => {
        if (error.code === 'ENOENT' && index + 1 < commands.length) attempt(index + 1);
        else reject(error);
      });
    };
    attempt(0);
  });
}

function createWindow(url) {
  mainWindow = new BrowserWindow({
    width: 1440,
    height: 920,
    minWidth: 800,
    minHeight: 560,
    backgroundColor: '#090807',
    title: 'Teenix',
    frame: false,
    titleBarStyle: 'hidden',
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, 'preload.cjs'),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  mainWindow.webContents.setWindowOpenHandler(({ url: target }) => {
    if (target.startsWith('https://')) shell.openExternal(target);
    return { action: 'deny' };
  });
  mainWindow.webContents.on('before-input-event', (event, input) => {
    if (input.type !== 'keyDown' || !(input.control || input.meta)) return;
    const zoomIn = ['+', '='].includes(input.key)
      || input.code === 'NumpadAdd'
      || (input.code === 'Equal' && input.shift);
    const zoomOut = input.key === '-' || input.code === 'NumpadSubtract';
    const resetZoom = input.key === '0' || input.code === 'Numpad0';
    if (!zoomIn && !zoomOut && !resetZoom) return;
    event.preventDefault();
    const current = mainWindow.webContents.getZoomFactor();
    const next = resetZoom ? 1 : current + (zoomIn ? 0.1 : -0.1);
    mainWindow.webContents.setZoomFactor(Math.min(2, Math.max(0.5, next)));
  });
  mainWindow.webContents.on('will-navigate', (event, target) => {
    if (!target.startsWith('http://127.0.0.1:')) event.preventDefault();
  });
  const sendMaximizeState = () => {
    if (!mainWindow?.isDestroyed()) {
      mainWindow.webContents.send('teenix-window:maximize-state', mainWindow.isMaximized());
    }
  };
  mainWindow.on('maximize', sendMaximizeState);
  mainWindow.on('unmaximize', sendMaximizeState);
  mainWindow.loadURL(url);
  mainWindow.on('closed', () => { mainWindow = null; });
}

function handleWindowAction(event, action) {
  if (!mainWindow || mainWindow.isDestroyed() || event.sender !== mainWindow.webContents) return;
  if (action === 'minimize') mainWindow.minimize();
  else if (action === 'toggle-maximize') {
    if (mainWindow.isMaximized()) mainWindow.unmaximize();
    else mainWindow.maximize();
  } else if (action === 'close') mainWindow.close();
}

ipcMain.on('teenix-window:minimize', event => handleWindowAction(event, 'minimize'));
ipcMain.on('teenix-window:toggle-maximize', event => handleWindowAction(event, 'toggle-maximize'));
ipcMain.on('teenix-window:close', event => handleWindowAction(event, 'close'));

app.whenReady().then(async () => {
  try {
    // Keep the origin stable so Chromium localStorage (themes, settings and profile)
    // survives app restarts. Set STREAMSCRAP_PORT only when another service uses 8000.
    const port = Number(process.env.TEENIX_PORT || process.env.STREAMSCRAP_PORT || 8000);
    if (!Number.isInteger(port) || port < 1 || port > 65535) {
      throw new Error('TEENIX_PORT deve essere un numero di porta valido.');
    }
    const root = app.isPackaged ? process.resourcesPath : app.getAppPath();
    const serverPath = path.join(root, 'server.py');
    backend = await startBackend(serverPath, root, port);
    await waitForBackend(port, backend);
    createWindow(`http://127.0.0.1:${port}/`);
    if (app.isPackaged) {
      autoUpdater.autoDownload = true;
      autoUpdater.autoInstallOnAppQuit = true;
      autoUpdater.on('error', error => console.error('Controllo aggiornamenti Teenix fallito.', error));
      autoUpdater.on('update-downloaded', () => {
        setTimeout(() => autoUpdater.quitAndInstall(true, true), 1200);
      });
      autoUpdater.checkForUpdates().catch(error => {
        console.error('Controllo aggiornamenti Teenix fallito.', error);
      });
    }
  } catch (error) {
    dialog.showErrorBox('Teenix non si è avviato', error.message);
    app.quit();
  }
});

app.on('before-quit', () => {
  if (backend && backend.exitCode === null) backend.kill();
});

app.on('window-all-closed', () => {
  if (process.platform !== 'darwin') app.quit();
});

app.on('activate', () => {
  if (!mainWindow) app.quit();
});
