const { contextBridge, ipcRenderer } = require('electron');

contextBridge.exposeInMainWorld('teenixWindow', {
  isElectron: true,
  minimize: () => ipcRenderer.send('teenix-window:minimize'),
  toggleMaximize: () => ipcRenderer.send('teenix-window:toggle-maximize'),
  close: () => ipcRenderer.send('teenix-window:close'),
  onMaximizeChange: callback => {
    ipcRenderer.on('teenix-window:maximize-state', (_event, maximized) => {
      callback(Boolean(maximized));
    });
  },
});
