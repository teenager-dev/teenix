const $ = id => document.getElementById(id);
if (window.teenixWindow?.isElectron) document.documentElement.classList.add('electron-app');
const isTvDevice = /TeenixTV/.test(navigator.userAgent);
if (isTvDevice) document.body.classList.add('tv-mode');
window.setTimeout(() => {
  const splash = $('startup-splash');
  if (!splash) return;
  splash.classList.add('is-exiting');
  window.setTimeout(() => splash.remove(), 650);
}, 1100);

const elements = {
  source: $('source'),
  profileScreen: $('profile-screen'),
  aboutScreen: $('about-screen'),
  settingsScreen: $('settings-screen'),
  profileList: $('profile-list'),
  profileControls: $('profile-controls'),
  page: document.querySelector('.page-shell'),
  profileManager: $('profile-manager'),
  profileForm: $('profile-form'),
  profileError: $('profile-form-error'),
  managerError: $('profile-manager-error'),
  screenError: $('profile-screen-error'),
  kind: $('kind'),
  kindWrap: $('kind-wrap'),
  kindSelect: $('kind-select'),
  kindToggle: $('kind-toggle'),
  kindLabel: $('kind-label'),
  kindMenu: document.querySelector('#kind-select .custom-select-menu'),
  query: $('q'),
  searchForm: $('search-form'),
  searchScreen: $('search-screen'),
  searchResults: $('search-results'),
  searchStatus: $('search-status'),
  searchResultsCount: $('search-results-count'),
  searchTitle: $('search-title'),
  results: $('results'),
  status: $('status'),
  resultsCount: $('results-count'),
  catalogTitle: $('catalog-title'),
  catalogSection: document.querySelector('.catalog-section'),
  detail: $('title-detail'),
  episodes: $('episodes'),
  episodesTitle: $('episodes-title'),
  episodesList: $('episodes-list'),
  player: $('player'),
  video: $('video'),
  playerCenterPlay: $('player-center-play'),
  playingTitle: $('playing-title'),
  sourceInfo: $('source-info'),
  playerStatus: $('player-status'),
  playerLoading: $('player-loading'),
  playerPlay: $('player-play'),
  playerSeek: $('player-seek'),
  playerCurrentTime: $('player-current-time'),
  playerDuration: $('player-duration'),
  playerVolume: $('player-volume'),
  playerQuality: $('player-quality'),
  loading: $('loading-overlay'),
  loadingMessage: $('loading-message'),
  catalogHero: $('catalog-hero'),
  heroImage: $('catalog-hero-image'),
  heroTitle: $('catalog-hero-title'),
  heroDescription: $('catalog-hero-description'),
};

const PROFILE_STORAGE = 'streamscrap.profiles.v1';
const ACTIVE_PROFILE_STORAGE = 'streamscrap.activeProfile.v1';
const SOURCE_STORAGE = 'streamscrap.catalogSource.v1';
const FAVORITES_STORAGE = 'streamscrap.favorites.v1';
const MOTION_STORAGE = 'streamscrap.reduceMotion.v1';
const THEME_STORAGE = 'streamscrap.theme.v1';
const RESUME_STORAGE = 'streamscrap.resume.v1';
const AUDIO_STORAGE = 'streamscrap.preferredAudio.v1';
const PROFILE_IMAGE_LIMIT = 2 * 1024 * 1024;
const b64 = value => btoa(unescape(encodeURIComponent(value)))
  .replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
const proxied = (url, referer = '') => {
  const raw = String(url || '').trim();
  const target = raw.startsWith('//') ? `${location.protocol}${raw}` : raw;
  if (!/^https?:\/\//i.test(target)) return '';
  return `/api/proxy?u=${b64(target)}${referer ? `&r=${b64(referer)}` : ''}`;
};

function useImageFallback(image, url, referer = '') {
  let attemptedProxy = false;
  image.onerror = () => {
    const fallback = !attemptedProxy ? proxied(url, referer) : '';
    attemptedProxy = true;
    if (fallback) {
      image.src = fallback;
      return;
    }
    image.onerror = null;
    image.removeAttribute('src');
    image.classList.add('cover-placeholder');
  };
}

function preferredAudio() {
  const saved = localStorage.getItem(AUDIO_STORAGE) || 'it';
  return ['it', 'original', 'auto'].includes(saved) ? saved : 'it';
}

let profiles = loadProfiles();
let activeProfileId = localStorage.getItem(ACTIVE_PROFILE_STORAGE);
let editingProfileId = null;
let pendingAvatar = '';
let settingsReturnToPicker = false;
let currentDetail = null;
let currentPlaybackUrl = '';
let currentPlaybackMp4 = '';
let currentPlaybackSource = '';
let currentPlaybackReferer = '';
let currentPlaybackVariants = [];
let playerQualitySources = [];
let playbackController = null;
let pendingSeekTime = null;
let nativeRecoveryAttempts = 0;
let hlsRecoveryTimer = null;
let hlsRecoveryAttempts = { network: 0, media: 0 };
let playerControlsTimer = null;
let themeTransitionTimer = null;
let searchController = null;
let searchTimer = null;
let loadingCount = 0;
const searchCache = new Map();
let catalogRequest = 0;
let hls = null;
let lastCatalogHash = '#/catalog/sc';
let lastCatalogSections = [];
let showingFavorites = false;
let homeMoreCursors = { sc: null, movies: null };
let homeMoreLoading = false;
let homeMoreFailed = false;
let homeLoadedIds = new Set();
let homeMoreObserver = null;
let homeMoreEpoch = 0;
let pageScrollFrame = 0;
let pageScrollHideTimer = null;
let updatePageScrollbar = () => {};
let favorites = loadFavorites();
let resumeByProfile = loadResume();
let resumeContext = null;
let resumeSeekTime = 0;
let lastResumeWrite = 0;
let railSequence = 0;
let selectSequence = 0;
let cardObserver = null;
let firstCardReveal = true;

function loadResume() {
  try {
    const stored = JSON.parse(localStorage.getItem(RESUME_STORAGE) || '{}');
    return stored && typeof stored === 'object' && !Array.isArray(stored) ? stored : {};
  } catch (error) {
    console.error('Impossibile leggere i titoli da riprendere.', error);
    return {};
  }
}

function resumeItems() {
  const key = activeProfileId || 'guest';
  return Array.isArray(resumeByProfile[key]) ? resumeByProfile[key] : [];
}

function saveResumeItems(items) {
  const key = activeProfileId || 'guest';
  resumeByProfile[key] = items.slice(0, 30);
  try {
    localStorage.setItem(RESUME_STORAGE, JSON.stringify(resumeByProfile));
  } catch (error) {
    console.error('Impossibile salvare la visione in corso.', error);
    showStatus('Impossibile salvare la visione in corso nel browser.', true);
  }
}

function resumeKey(entry) {
  return `${entry.kind}:${entry.id || entry.slug}:${entry.season || 1}:${entry.episode || 1}`;
}

function persistResumeProgress(force = false) {
  if (!resumeContext || elements.video.currentTime < 5) return;
  const now = Date.now();
  if (!force && now - lastResumeWrite < 7000) return;
  lastResumeWrite = now;
  const duration = Number.isFinite(elements.video.duration) ? elements.video.duration : 0;
  const entry = {
    ...resumeContext,
    progress: duration > 0 ? Math.min(99, elements.video.currentTime / duration * 100) : 0,
    currentTime: elements.video.currentTime,
    duration,
    updatedAt: now,
  };
  const key = resumeKey(entry);
  saveResumeItems([entry, ...resumeItems().filter(item => resumeKey(item) !== key)]);
  if (elements.results.classList.contains('home-results')) renderResumeRail();
}

function removeResumeItem(entry) {
  saveResumeItems(resumeItems().filter(item => resumeKey(item) !== resumeKey(entry)));
  renderResumeRail();
}

function loadFavorites() {
  try {
    const saved = JSON.parse(localStorage.getItem(FAVORITES_STORAGE) || '{}');
    return saved && typeof saved === 'object' && !Array.isArray(saved) ? saved : {};
  } catch (error) {
    console.error('Impossibile leggere la lista personale.', error);
    return {};
  }

}

function favoriteItems() {
  const key = activeProfileId || 'guest';
  return Array.isArray(favorites[key]) ? favorites[key] : [];
}

function saveFavorites(items) {
  const key = activeProfileId || 'guest';
  favorites[key] = items;
  try {
    localStorage.setItem(FAVORITES_STORAGE, JSON.stringify(favorites));
  } catch (error) {
    console.error('Impossibile salvare la lista personale.', error);
    showStatus('La lista personale non può essere salvata in questo browser.', true);
  }
}

function loadProfiles() {
  try {
    const stored = JSON.parse(localStorage.getItem(PROFILE_STORAGE) || '[]');
    return Array.isArray(stored) ? stored.filter(profile =>
      profile && typeof profile.id === 'string' && typeof profile.name === 'string') : [];
  } catch (error) {
    console.error('Impossibile leggere i profili salvati.', error);
    return [];
  }
}

function saveProfiles() {
  try {
    localStorage.setItem(PROFILE_STORAGE, JSON.stringify(profiles));
    if (activeProfileId) localStorage.setItem(ACTIVE_PROFILE_STORAGE, activeProfileId);
    else localStorage.removeItem(ACTIVE_PROFILE_STORAGE);
  } catch (error) {
    throw new Error('Impossibile salvare il profilo nel browser.');
  }
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function showStatus(message, isError = false) {
  elements.status.textContent = message;
  elements.status.classList.toggle('error', isError);
  elements.status.classList.remove('hidden');
}

function showRetryStatus(message, retry) {
  elements.status.replaceChildren(document.createTextNode(message));
  const button = el('button', 'status-retry', 'Riprova');
  button.type = 'button';
  button.addEventListener('click', retry, { once: true });
  elements.status.append(' ', button);
  elements.status.classList.add('error');
  elements.status.classList.remove('hidden');
}

function clearStatus() {
  elements.status.textContent = '';
  elements.status.classList.add('hidden');
  elements.status.classList.remove('error');
}

function setLoading(message, active) {
  if (active) {
    loadingCount += 1;
    elements.loadingMessage.textContent = message;
    elements.loading.classList.remove('hidden');
    elements.loading.setAttribute('aria-hidden', 'false');
  } else {
    loadingCount = Math.max(0, loadingCount - 1);
    if (!loadingCount) {
      elements.loading.classList.add('hidden');
      elements.loading.setAttribute('aria-hidden', 'true');
    }
  }
}

async function withLoading(message, callback) {
  setLoading(message, true);
  try {
    return await callback();
  } finally {
    setLoading(message, false);
  }
}

async function api(path, signal) {
  const response = await fetch(path, { signal });
  let data;
  try {
    data = await response.json();
  } catch (error) {
    if (!response.ok) throw new Error(`Richiesta non riuscita (${response.status}).`);
    throw new Error('La risposta del server non è un JSON valido.');
  }
  if (!response.ok) {
    const detail = data.detail;
    const message = typeof detail === 'string' ? detail
      : Array.isArray(detail) ? detail.map(item => item.msg || JSON.stringify(item)).join(' · ')
      : detail && typeof detail === 'object' ? JSON.stringify(detail)
      : `Richiesta non riuscita (${response.status}).`;
    throw new Error(message);
  }
  return data;
}

function initials(name) {
  return name.trim().split(/\s+/).slice(0, 2).map(part => part[0] || '').join('').toUpperCase() || '?';
}

function setAvatar(image, fallback, profile, imageClass, fallbackClass) {
  if (profile.avatar) {
    image.src = profile.avatar;
    image.alt = `Immagine profilo di ${profile.name}`;
    image.className = imageClass;
    fallback.className = `${fallbackClass} hidden`;
  } else {
    image.removeAttribute('src');
    image.className = `${imageClass} hidden`;
    fallback.textContent = initials(profile.name);
    fallback.className = fallbackClass;
  }
}

function showProfileScreenError(message = '') {
  elements.screenError.textContent = message;
  elements.screenError.classList.toggle('hidden', !message);
}

function renderProfilePicker() {
  elements.profileList.replaceChildren();
  showProfileScreenError();
  profiles.forEach(profile => {
    const card = el('button', 'profile-card');
    card.type = 'button';
    const avatar = el('img', 'profile-avatar');
    const fallback = el('span', 'profile-avatar-fallback');
    setAvatar(avatar, fallback, profile, 'profile-avatar', 'profile-avatar-fallback');
    card.append(avatar, fallback, el('span', 'profile-card-name', profile.name));
    card.addEventListener('click', () => activateProfile(profile.id));
    elements.profileList.append(card);
  });
  const add = el('button', 'profile-card profile-card-add');
  add.type = 'button';
  add.append(el('span', 'profile-add-icon', '+'), el('span', 'profile-card-name', 'Aggiungi profilo'));
  add.addEventListener('click', () => openProfileForm());
  elements.profileList.append(add);
}

function activateProfile(profileId, { load = true } = {}) {
  const profile = profiles.find(item => item.id === profileId);
  if (!profile) {
    showProfileScreenError('Il profilo selezionato non è disponibile.');
    return;
  }
  activeProfileId = profile.id;
  try {
    saveProfiles();
  } catch (error) {
    showProfileScreenError(error.message);
    return;
  }
  elements.profileScreen.classList.add('hidden');
  elements.page.classList.remove('hidden');
  elements.profileControls.classList.remove('hidden');
  document.body.classList.remove('profile-picker-open');
  document.body.classList.add('page-ready');
  updateActiveProfile();
  if (load) showAbout();
}

function activateGuest({ load = true } = {}) {
  activeProfileId = null;
  elements.profileScreen.classList.add('hidden');
  elements.page.classList.remove('hidden');
  elements.profileControls.classList.remove('hidden');
  document.body.classList.remove('profile-picker-open');
  document.body.classList.add('page-ready');
  $('active-profile-avatar').classList.add('hidden');
  $('active-profile-initials').textContent = 'O';
  $('active-profile-initials').classList.remove('hidden');
  $('active-profile-name').textContent = 'Ospite';
  if (load) showAbout();
}

function updateActiveProfile() {
  const profile = profiles.find(item => item.id === activeProfileId);
  if (!profile) return;
  setAvatar($('active-profile-avatar'), $('active-profile-initials'), profile,
    'header-avatar', 'header-avatar-fallback');
  $('active-profile-name').textContent = profile.name;
}

function renderManageProfiles() {
  const list = $('manage-profile-list');
  list.replaceChildren();
  elements.managerError.textContent = '';
  elements.managerError.classList.add('hidden');
  profiles.forEach(profile => {
    const row = el('div', 'manage-profile-row');
    const avatar = el('img', 'manage-avatar');
    const fallback = el('span', 'manage-avatar-fallback');
    setAvatar(avatar, fallback, profile, 'manage-avatar', 'manage-avatar-fallback');
    const name = el('span', 'manage-profile-name', profile.name);
    const actions = el('div', 'manage-profile-actions');
    const edit = el('button', 'text-button', 'Modifica');
    edit.type = 'button';
    edit.addEventListener('click', () => openProfileForm(profile.id));
    const remove = el('button', 'text-button danger-button', 'Elimina');
    remove.type = 'button';
    remove.disabled = profiles.length < 2;
    remove.addEventListener('click', () => deleteProfile(profile.id));
    actions.append(edit, remove);
    row.append(avatar, fallback, name, actions);
    list.append(row);
  });
  $('switch-profile').classList.toggle('hidden', !activeProfileId);
}

function openSettingsPage() {
  settingsReturnToPicker = !elements.profileScreen.classList.contains('hidden');
  elements.profileScreen.classList.add('hidden');
  document.body.classList.remove('profile-picker-open');
  elements.page.classList.add('hidden');
  elements.settingsScreen.classList.remove('hidden');
  renderManageProfiles();
  elements.profileManager.classList.remove('hidden');
  elements.profileForm.classList.add('hidden');
  document.body.classList.add('settings-open');
  document.querySelectorAll('.sidebar-icon').forEach(button => button.classList.remove('is-active'));
  $('sidebar-settings').classList.add('is-active');
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

function closeSettingsPage() {
  elements.settingsScreen.classList.add('hidden');
  document.body.classList.remove('settings-open');
  if (settingsReturnToPicker || !activeProfileId) {
    elements.profileScreen.classList.remove('hidden');
    document.body.classList.add('profile-picker-open');
  } else {
    elements.page.classList.remove('hidden');
    updateActiveProfile();
  }
}

function openProfileForm(profileId = null) {
  editingProfileId = profileId;
  const profile = profiles.find(item => item.id === profileId);
  pendingAvatar = profile?.avatar || '';
  $('profile-form-title').textContent = profile ? 'Modifica profilo' : 'Crea un profilo';
  $('profile-name').value = profile?.name || '';
  $('avatar-file').value = '';
  $('remove-avatar').disabled = !pendingAvatar;
  updateAvatarPreview();
  elements.profileError.textContent = '';
  elements.profileError.classList.add('hidden');
  elements.profileManager.classList.add('hidden');
  elements.profileForm.classList.remove('hidden');
  if (elements.settingsScreen.classList.contains('hidden')) {
    settingsReturnToPicker = !elements.profileScreen.classList.contains('hidden');
    elements.profileScreen.classList.add('hidden');
    document.body.classList.remove('profile-picker-open');
    elements.page.classList.add('hidden');
    elements.settingsScreen.classList.remove('hidden');
    document.body.classList.add('settings-open');
  }
  $('profile-name').focus();
}

function updateAvatarPreview() {
  const image = $('avatar-preview');
  const fallback = $('avatar-preview-fallback');
  if (pendingAvatar) {
    image.src = pendingAvatar;
    image.classList.remove('hidden');
    fallback.classList.add('hidden');
  } else {
    image.removeAttribute('src');
    image.classList.add('hidden');
    fallback.textContent = initials($('profile-name').value || '?');
    fallback.classList.remove('hidden');
  }
}

function showProfileFormError(message) {
  elements.profileError.textContent = message;
  elements.profileError.classList.remove('hidden');
}

function deleteProfile(profileId) {
  if (profiles.length < 2) {
    elements.managerError.textContent = 'Deve esserci almeno un profilo.';
    elements.managerError.classList.remove('hidden');
    return;
  }
  const deletingActive = profileId === activeProfileId;
  const previousActiveProfileId = activeProfileId;
  profiles = profiles.filter(profile => profile.id !== profileId);
  if (deletingActive) activeProfileId = profiles[0]?.id || null;
  try {
    saveProfiles();
  } catch (error) {
    elements.managerError.textContent = error.message;
    elements.managerError.classList.remove('hidden');
    profiles = loadProfiles();
    activeProfileId = previousActiveProfileId;
    return;
  }
  renderManageProfiles();
  if (deletingActive && activeProfileId) {
    updateActiveProfile();
    closeSettingsPage();
    renderProfilePicker();
    activateProfile(activeProfileId);
  }
}

function saveProfile(event) {
  event.preventDefault();
  const name = $('profile-name').value.trim();
  if (!name) {
    showProfileFormError('Inserisci un nome per il profilo.');
    return;
  }
  const existing = profiles.find(profile => profile.id === editingProfileId);
  const hadActiveProfile = Boolean(activeProfileId);
  const profile = {
    id: existing?.id || (crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`),
    name,
    avatar: pendingAvatar,
  };
  profiles = existing
    ? profiles.map(item => item.id === profile.id ? profile : item)
    : [...profiles, profile];
  if (!activeProfileId) activeProfileId = profile.id;
  try {
    saveProfiles();
  } catch (error) {
    profiles = loadProfiles();
    showProfileFormError(error.message);
    return;
  }
  if (activeProfileId === profile.id) updateActiveProfile();
  renderProfilePicker();
  if (!hadActiveProfile) {
    closeSettingsPage();
    activateProfile(profile.id);
    return;
  }
  openSettingsPage();
}

function showCatalog() {
  currentDetail = null;
  elements.aboutScreen.classList.add('hidden');
  elements.settingsScreen.classList.add('hidden');
  document.body.classList.remove('settings-open');
  elements.searchScreen.classList.add('hidden');
  elements.detail.classList.add('hidden');
  elements.episodes.classList.add('hidden');
  elements.catalogSection.classList.remove('hidden');
  elements.catalogHero.classList.add('hidden');
  $('detail-back-label').textContent = 'Torna ai risultati';
}

function showAbout({ syncRoute = true } = {}) {
  showingFavorites = false;
  showCatalog();
  elements.page.classList.remove('hidden');
  elements.aboutScreen.classList.remove('hidden');
  elements.searchScreen.classList.add('hidden');
  elements.catalogSection.classList.add('hidden');
  elements.catalogHero.classList.add('hidden');
  elements.detail.classList.add('hidden');
  elements.episodes.classList.add('hidden');
  $('related-panel').classList.add('hidden');
  document.querySelectorAll('.sidebar-icon').forEach(button => button.classList.remove('is-active'));
  $('sidebar-home').classList.add('is-active');
  lastCatalogHash = '#/about';
  if (syncRoute) history.replaceState(null, '', lastCatalogHash);
}

function showSearchScreen() {
  currentDetail = null;
  elements.aboutScreen.classList.add('hidden');
  elements.detail.classList.add('hidden');
  elements.episodes.classList.add('hidden');
  elements.catalogSection.classList.add('hidden');
  elements.catalogHero.classList.add('hidden');
  elements.searchScreen.classList.remove('hidden');
  document.querySelectorAll('.sidebar-icon').forEach(button => button.classList.remove('is-active'));
  $('sidebar-search').classList.add('is-active');
}

function switchSearchSource(source) {
  setSource(source, { refresh: false });
  showSearchScreen();
  const query = elements.query.value.trim();
  lastCatalogHash = query.length >= 2
    ? `#/search/${source}?${new URLSearchParams({ q: query, kind: elements.kind.value })}`
    : `#/search/${source}`;
  history.replaceState(null, '', lastCatalogHash);
  if (query.length >= 2) runSearch(query, { syncRoute: false });
  else {
    elements.searchResults.replaceChildren();
    elements.searchResultsCount.textContent = '';
    elements.searchTitle.textContent = 'I risultati appariranno qui';
    showSearchStatus(source === 'au'
      ? 'Cerca un anime per vedere i risultati.'
      : 'Cerca un film o una serie TV per vedere i risultati.');
  }
}

function applyTheme(theme) {
  const themes = ['orange', 'black', 'white', 'blue', 'purple', 'green', 'red', 'rose', 'teal', 'gold'];
  const selected = themes.includes(theme) ? theme : 'orange';
  const previous = document.body.dataset.theme;
  document.body.dataset.theme = selected;
  document.body.classList.toggle('theme-black', selected === 'black');
  document.body.classList.toggle('theme-white', selected === 'white');
  const themeColor = ({ white: '#f4f6f8', black: '#090b0e', blue: '#071726', purple: '#120d20', green: '#091a14', red: '#090909', rose: '#1a0d17', teal: '#081a1b', gold: '#191509' })[selected] || '#100b07';
  document.querySelector('meta[name="theme-color"]').content = themeColor;
  document.querySelectorAll('[data-theme-option]').forEach(button => {
    const active = button.dataset.themeOption === selected;
    button.classList.toggle('is-selected', active);
    button.setAttribute('aria-pressed', String(active));
  });
  if (previous && previous !== selected) {
    document.body.style.setProperty('--theme-transition-color', ({
      orange: '#ff861f', black: '#dcecff', white: '#53677f', blue: '#35aaff',
      purple: '#ae83ff', green: '#47d29c', red: '#ff5268', rose: '#ff73c7',
      teal: '#43d8d0', gold: '#f2c56b',
    })[selected]);
    document.body.classList.remove('theme-changing');
    void document.body.offsetWidth;
    document.body.classList.add('theme-changing');
    clearTimeout(themeTransitionTimer);
    themeTransitionTimer = setTimeout(() => document.body.classList.remove('theme-changing'), 760);
  }
}

function enhanceSelect(select) {
  if (!select) return;
  if (select.dataset.customized === 'true') {
    select.customSelectUpdate?.();
    return;
  }
  const wrapper = el('div', `custom-select ${select.classList.contains('episode-season') ? 'season-select' : ''}`);
  const toggle = el('button', 'custom-select-toggle');
  toggle.type = 'button';
  toggle.setAttribute('aria-haspopup', 'listbox');
  toggle.setAttribute('aria-expanded', 'false');
  const label = el('span', 'custom-select-value');
  const arrow = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  arrow.setAttribute('viewBox', '0 0 20 20');
  arrow.setAttribute('fill', 'none');
  arrow.setAttribute('aria-hidden', 'true');
  const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
  path.setAttribute('d', 'm5 7.5 5 5 5-5');
  path.setAttribute('stroke', 'currentColor');
  path.setAttribute('stroke-width', '1.7');
  path.setAttribute('stroke-linecap', 'round');
  path.setAttribute('stroke-linejoin', 'round');
  arrow.append(path);
  toggle.append(label, arrow);
  const menu = el('div', 'custom-select-menu hidden');
  menu.id = `custom-select-menu-${++selectSequence}`;
  menu.setAttribute('role', 'listbox');
  toggle.setAttribute('aria-controls', menu.id);
  const update = () => {
    label.textContent = select.selectedOptions[0]?.textContent || '';
    toggle.disabled = select.disabled;
    menu.replaceChildren();
    [...select.options].forEach((option, index) => {
      const item = el('button', `custom-option ${option.selected ? 'is-selected' : ''}`, option.textContent);
      item.type = 'button';
      item.setAttribute('role', 'option');
      item.setAttribute('aria-selected', String(option.selected));
      item.disabled = option.disabled;
      item.addEventListener('click', () => {
        select.selectedIndex = index;
        select.dispatchEvent(new Event('change', { bubbles: true }));
        update();
        menu.classList.add('hidden');
        wrapper.classList.remove('is-open');
        toggle.setAttribute('aria-expanded', 'false');
        toggle.focus();
      });
      menu.append(item);
    });
  };
  toggle.addEventListener('click', () => {
    const open = menu.classList.contains('hidden');
    document.querySelectorAll('.custom-select.is-open').forEach(other => {
      other.classList.remove('is-open');
      other.querySelector('.custom-select-menu')?.classList.add('hidden');
      other.querySelector('.custom-select-toggle')?.setAttribute('aria-expanded', 'false');
    });
    menu.classList.toggle('hidden', !open);
    wrapper.classList.toggle('is-open', open);
    toggle.setAttribute('aria-expanded', String(open));
  });
  toggle.addEventListener('keydown', event => {
    if (event.key === 'ArrowDown' || event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      if (menu.classList.contains('hidden')) toggle.click();
      menu.querySelector('.custom-option:not(:disabled)')?.focus();
    } else if (event.key === 'Escape') {
      menu.classList.add('hidden');
      wrapper.classList.remove('is-open');
      toggle.setAttribute('aria-expanded', 'false');
    }
  });
  select.addEventListener('change', update);
  select.customSelectUpdate = update;
  select.after(wrapper);
  wrapper.append(toggle, menu);
  select.classList.add('native-select');
  select.dataset.customized = 'true';
  update();
}

document.addEventListener('click', event => {
  if (event.target.closest('.custom-select')) return;
  document.querySelectorAll('.custom-select.is-open').forEach(wrapper => {
    wrapper.classList.remove('is-open');
    wrapper.querySelector('.custom-select-menu')?.classList.add('hidden');
    wrapper.querySelector('.custom-select-toggle')?.setAttribute('aria-expanded', 'false');
  });
});

function showSearchStatus(message, isError = false, retry) {
  elements.searchStatus.replaceChildren(document.createTextNode(message));
  elements.searchStatus.classList.toggle('error', isError);
  elements.searchStatus.classList.remove('hidden');
  if (retry) {
    const button = el('button', 'status-retry', 'Riprova');
    button.type = 'button';
    button.addEventListener('click', retry, { once: true });
    elements.searchStatus.append(' ', button);
  }
}

function clearSearchStatus() {
  elements.searchStatus.textContent = '';
  elements.searchStatus.classList.add('hidden');
  elements.searchStatus.classList.remove('error');
}

function setSource(source, { refresh = true, persist = true } = {}) {
  elements.source.value = source;
  const animeMode = source === 'au';
  document.body.classList.toggle('is-anime-mode', animeMode);
  $('nav-film-tv').setAttribute('aria-pressed', String(!animeMode));
  $('nav-anime').setAttribute('aria-pressed', String(animeMode));
  $('nav-film-tv').classList.toggle('is-active', !animeMode);
  $('nav-anime').classList.toggle('is-active', animeMode);
  $('sidebar-home').classList.remove('is-active');
  $('sidebar-favorites').classList.remove('is-active');
  $('sidebar-search').classList.remove('is-active');
  elements.kindWrap.classList.toggle('hidden', animeMode);
  $('search-source-sc').setAttribute('aria-pressed', String(!animeMode));
  $('search-source-au').setAttribute('aria-pressed', String(animeMode));
  if (animeMode) setKind('all');
  elements.catalogTitle.textContent = animeMode ? 'Anime in evidenza' : 'Novità';
  searchCache.clear();
  if (persist) {
    try {
      localStorage.setItem(SOURCE_STORAGE, source);
    } catch (error) {
      console.warn('Impossibile salvare la sezione selezionata.', error);
    }
  }
  if (!refresh || elements.page.classList.contains('hidden')) return;
  showCatalog();
  if (elements.query.value.trim().length >= 2) runSearch(elements.query.value.trim());
  else loadHome();
}

function switchCatalog(source) {
  closeSidebar();
  elements.query.value = '';
  setKind('all');
  setSource(source);
}

function setKind(value) {
  elements.kind.value = value;
  const selected = [...elements.kindMenu.querySelectorAll('.custom-option')]
    .find(option => option.dataset.value === value);
  if (selected) {
    elements.kindLabel.textContent = selected.textContent;
    elements.kindMenu.querySelectorAll('.custom-option').forEach(option => {
      const active = option === selected;
      option.classList.toggle('is-selected', active);
      option.setAttribute('aria-selected', String(active));
    });
  }
}

function filterItems(items) {
  const animeMode = elements.source.value === 'au';
  const filtered = (items || []).filter(item =>
    animeMode ? item.kind === 'anime' : item.kind !== 'anime');
  if (animeMode || elements.kind.value === 'all') return filtered;
  const requested = elements.kind.value;
  return filtered.filter(item => {
    const meta = String(item.meta || '').toLowerCase();
    if (!meta) return true;
    return requested === 'movie'
      ? /film|movie|cinema/.test(meta)
      : /serie|series|tv|show/.test(meta);
  });
}

function makeCard(item, { onOpen, resumeEntry } = {}) {
  const type = item.kind === 'anime' ? 'anime' : 'serie';
  const href = type === 'anime'
    ? animeRoute(item)
    : `#/serie/${encodeURIComponent(item.id)}/${encodeURIComponent(item.slug || '')}`;
  const card = el('article', 'card');
  const link = el('a', 'card-link');
  link.href = onOpen ? '#' : href;
  if (onOpen) {
    link.addEventListener('click', event => {
      event.preventDefault();
      onOpen();
    });
  }
  const image = document.createElement('img');
  image.className = 'card-cover';
  image.alt = '';
  image.loading = 'lazy';
  if (item.poster) {
    image.src = item.poster;
    useImageFallback(image, item.poster);
  } else image.classList.add('cover-placeholder');
  const info = el('div', 'card-info');
  info.append(el('span', 'name', item.title || 'Titolo senza nome'));
  const meta = [item.meta, item.rating ? `★ ${item.rating}` : ''].filter(Boolean).join(' · ');
  info.append(el('span', 'meta', item.ep ? `Episodio ${item.ep}` : meta || (type === 'anime' ? 'Anime' : 'Film & TV')));
  link.append(image, info);
  const isFavorite = favoriteItems().some(saved =>
    saved.kind === item.kind && String(saved.id || saved.slug) === String(item.id || item.slug));
  const favorite = el('button', `favorite-toggle${isFavorite ? ' is-favorite' : ''}`, isFavorite ? '♥' : '♡');
  favorite.type = 'button';
  favorite.setAttribute('aria-label', isFavorite ? `Rimuovi ${item.title} dalla lista` : `Aggiungi ${item.title} alla lista`);
  favorite.setAttribute('aria-pressed', String(isFavorite));
  favorite.addEventListener('click', event => {
    event.preventDefault();
    event.stopPropagation();
    toggleFavorite(item);
  });
  card.append(link, favorite);
  if (resumeEntry) {
    const progress = el('span', 'resume-progress');
    const progressFill = el('span');
    progressFill.style.width = `${Math.max(0, Math.min(100, resumeEntry.progress || 0))}%`;
    progress.append(progressFill);
    const remove = el('button', 'resume-remove', '×');
    remove.type = 'button';
    remove.setAttribute('aria-label', `Rimuovi ${item.title} da Riprendi`);
    remove.addEventListener('click', event => {
      event.preventDefault();
      event.stopPropagation();
      removeResumeItem(resumeEntry);
    });
    card.append(progress, remove);
  }
  return card;
}

function toggleFavorite(item) {
  const current = favoriteItems();
  const exists = current.some(saved =>
    saved.kind === item.kind && String(saved.id || saved.slug) === String(item.id || item.slug));
  const updated = exists
    ? current.filter(saved => !(saved.kind === item.kind && String(saved.id || saved.slug) === String(item.id || item.slug)))
    : [...current, {
      kind: item.kind,
      id: item.id,
      slug: item.slug,
      title: item.title,
      poster: item.poster,
      meta: item.meta,
      rating: item.rating,
      catalog_source: item.catalog_source,
      lookup_title: item.lookup_title,
    }];
  saveFavorites(updated);
  if (showingFavorites) renderFavorites();
  else if (!elements.searchScreen.classList.contains('hidden')) {
    const card = [...elements.searchResults.querySelectorAll('.card')]
      .find(node => node.querySelector('.name')?.textContent === item.title);
    if (card) card.replaceWith(makeCard(item));
  }
  else if (elements.results.classList.contains('home-results')) renderHome({ sections: lastCatalogSections });
  else {
    const cards = [...elements.results.querySelectorAll('.card')];
    const card = cards.find(node => node.querySelector('.name')?.textContent === item.title);
    if (card) {
      const replacement = makeCard(item);
      card.replaceWith(replacement);
    }
  }
}

function renderFavorites() {
  showingFavorites = true;
  showCatalog();
  elements.catalogHero.classList.add('hidden');
  elements.results.classList.remove('home-results');
  elements.catalogTitle.textContent = 'La mia lista';
  elements.resultsCount.textContent = `${favoriteItems().length} titoli`;
  elements.results.replaceChildren(...favoriteItems().map(makeCard));
  observeCards(elements.results);
  elements.query.value = '';
  if (favoriteItems().length) clearStatus();
  else showStatus('La tua lista è vuota. Aggiungi un titolo con il cuore sulle copertine.');
  document.querySelectorAll('.sidebar-icon').forEach(button => button.classList.remove('is-active'));
  $('sidebar-favorites').classList.add('is-active');
}

function renderResults(items, title, countLabel = '') {
  showingFavorites = false;
  showSearchScreen();
  elements.searchResults.replaceChildren(...items.map(makeCard));
  observeCards(elements.searchResults);
  elements.searchResultsCount.textContent = countLabel || (items.length ? `${items.length} titoli` : '');
  elements.searchTitle.textContent = title;
  clearSearchStatus();
}

function renderHome(data) {
  showingFavorites = false;
  $('sidebar-search').classList.remove('is-active');
  $('sidebar-favorites').classList.remove('is-active');
  const sections = (data.sections || []).map(section => ({
    ...section,
    items: filterItems(section.items),
  })).filter(section => section.items.length);
  if (data.next_cursors) {
    homeMoreCursors = {
      sc: Number.isInteger(data.next_cursors.sc) ? data.next_cursors.sc : null,
      movies: Number.isInteger(data.next_cursors.movies) ? data.next_cursors.movies : null,
    };
    homeMoreFailed = false;
  }
  lastCatalogSections = sections;
  homeLoadedIds = new Set(sections.flatMap(section => section.items)
    .map(item => `${item.kind}:${item.id || item.slug}`));
  const allItems = [...new Map(sections.flatMap(section => section.items)
    .map(item => [`${item.kind}:${item.id || item.slug}`, item])).values()];
  lastCatalogHash = `#/catalog/${elements.source.value}`;
  elements.resultsCount.textContent = allItems.length ? `${allItems.length} titoli` : '';
  elements.catalogTitle.textContent = elements.source.value === 'au' ? 'Anime da scoprire' : 'Novità';
  if (data.warnings?.length) showStatus(data.warnings.join(' '), true);
  else clearStatus();
  if (!allItems.length) {
    elements.results.replaceChildren();
    elements.catalogHero.classList.add('hidden');
    showStatus('Nessun titolo disponibile al momento.');
    return;
  }
  const featured = sections[0].items[0];
  if (featured) {
    elements.catalogHero.classList.remove('hidden');
    const animeMode = elements.source.value === 'au';
    $('catalog-hero-kicker').textContent = animeMode
      ? 'IN EVIDENZA · ANILIST'
      : 'IN EVIDENZA · FILM & TV';
    elements.heroTitle.textContent = featured.title;
    elements.heroDescription.textContent = featured.meta
      ? `${featured.meta} · Metadati da ${animeMode ? 'AniList' : 'fonti ufficiali'}`
      : `Scopri ${animeMode ? 'la scheda e gli episodi da AniList' : 'questo titolo nel catalogo Film & TV'}.`;
    if (featured.poster) {
      elements.heroImage.src = featured.poster;
      elements.heroImage.classList.remove('hidden');
      useImageFallback(elements.heroImage, featured.poster);
    } else {
      elements.heroImage.removeAttribute('src');
      elements.heroImage.classList.add('hidden');
    }
    $('catalog-hero-watch').onclick = () => {
      location.hash = featured.kind === 'anime'
        ? animeRoute(featured)
        : `#/serie/${encodeURIComponent(featured.id)}/${encodeURIComponent(featured.slug || '')}`;
    };
  } else {
    elements.catalogHero.classList.add('hidden');
  }
  elements.results.classList.add('home-results');
  elements.results.replaceChildren();
  renderResumeRail();
  for (const section of sections) {
    const rail = createRail(section.title, section.items);
    rail.dataset.homeSection = section.title;
    elements.results.append(rail);
  }
  setupHomeMoreSentinel();
  elements.catalogHero.classList.remove('is-featured-entering');
  requestAnimationFrame(() => elements.catalogHero.classList.add('is-featured-entering'));
  observeCards(elements.results);
  updatePageScrollbar();
}

function observeCards(container) {
  const cards = [...container.querySelectorAll('.card:not(.is-visible)')];
  if (!cards.length) return;
  if (document.body.classList.contains('reduce-motion') || !('IntersectionObserver' in window)) {
    cards.forEach(card => card.classList.add('is-visible'));
    return;
  }
  cardObserver ||= new IntersectionObserver(entries => {
    const visible = entries.filter(entry => entry.isIntersecting).map(entry => entry.target);
    if (!visible.length) return;
    animateCardBatch(visible);
  }, { threshold: 0.12 });
  const immediatelyVisible = cards.filter(card => {
    const rect = card.getBoundingClientRect();
    const rail = card.closest('.catalog-rail');
    const clip = rail?.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0 && rect.bottom > 0 && rect.top < window.innerHeight
      && rect.right > 0 && rect.left < window.innerWidth
      && (!clip || (rect.right > clip.left && rect.left < clip.right
        && rect.bottom > clip.top && rect.top < clip.bottom));
  });
  if (immediatelyVisible.length) animateCardBatch(immediatelyVisible);
  cards.filter(card => !card.classList.contains('is-visible'))
    .forEach(card => cardObserver.observe(card));
}

function animateCardBatch(cards) {
  const batch = [...new Set(cards)]
    .filter(card => card.isConnected && !card.classList.contains('is-visible'));
  batch.sort((left, right) => {
    const a = left.getBoundingClientRect();
    const b = right.getBoundingClientRect();
    return Math.abs(a.top - b.top) > 8 ? a.top - b.top : a.left - b.left;
  });
  if (!batch.length) return;
  const firstBatch = firstCardReveal;
  firstCardReveal = false;
  const duration = firstBatch ? 600 : 420;
  const lead = firstBatch ? 700 : 0;
  const total = firstBatch ? 5000 : Math.min(1400, Math.max(500, batch.length * 180));
  const step = batch.length > 1 ? Math.max(0, (total - lead - duration) / (batch.length - 1)) : 0;
  batch.forEach((card, index) => {
    card.classList.add('is-visible', 'is-entering');
    card.style.animationDelay = `${lead + step * index}ms`;
    card.style.animationDuration = `${duration}ms`;
    card.addEventListener('animationend', event => {
      if (event.animationName !== 'card-arrive') return;
      card.classList.remove('is-entering');
      card.style.removeProperty('animation-delay');
      card.style.removeProperty('animation-duration');
    }, { once: true });
    cardObserver?.unobserve(card);
  });
}

function animeRoute(item) {
  if (item.catalog_source === 'anilist') {
    return `#/anime/${encodeURIComponent(item.slug)}/${encodeURIComponent(item.lookup_title || item.title)}`;
  }
  return `#/anime/${encodeURIComponent(item.slug)}`;
}

function createRail(title, items, { resume = false } = {}) {
  const rail = el('section', 'catalog-rail-section');
  rail.setAttribute('aria-label', title);
  rail.id = `catalog-rail-${++railSequence}`;
  const heading = el('div', 'rail-heading');
  heading.append(el('h2', 'home-section-title', title));
  const controls = el('div', 'rail-controls');
  const left = el('button', 'rail-arrow rail-arrow-left', '‹');
  const right = el('button', 'rail-arrow rail-arrow-right', '›');
  left.type = right.type = 'button';
  left.setAttribute('aria-label', `Scorri ${title} a sinistra`);
  right.setAttribute('aria-label', `Scorri ${title} a destra`);
  controls.append(left, right);
  heading.append(controls);
  const cards = el('div', 'catalog-rail');
  cards.dataset.resume = String(resume);
  if (resume) {
    cards.append(...items.map(entry => makeCard(entry.item, {
      onOpen: () => resumePlayback(entry),
      resumeEntry: entry,
    })));
  } else {
    cards.append(...items.map(makeCard));
  }
  left.addEventListener('click', () => scrollRail(cards, -1));
  right.addEventListener('click', () => scrollRail(cards, 1));
  cards.addEventListener('scroll', () => updateRailButtons(cards, left, right), { passive: true });
  rail.append(heading, cards);
  requestAnimationFrame(() => updateRailButtons(cards, left, right));
  return rail;
}

function scrollRail(rail, direction) {
  const card = rail.querySelector('.card');
  const distance = card ? (card.getBoundingClientRect().width + 17) * 3 : rail.clientWidth * .8;
  rail.scrollBy({
    left: distance * direction,
    behavior: document.body.classList.contains('reduce-motion') ? 'auto' : 'smooth',
  });
}

function updateRailButtons(rail, left, right) {
  const maxScroll = rail.scrollWidth - rail.clientWidth;
  const canScrollLeft = maxScroll > 4 && rail.scrollLeft > 2;
  const canScrollRight = maxScroll > 4 && rail.scrollLeft < maxScroll - 2;
  left.classList.toggle('hidden', !canScrollLeft);
  right.classList.toggle('hidden', !canScrollRight);
  left.disabled = !canScrollLeft;
  right.disabled = !canScrollRight;
}

function renderResumeRail() {
  const old = $('results').querySelector('[data-resume-rail]');
  old?.remove();
  const entries = resumeItems().filter(entry =>
    entry.item && (entry.kind === 'anime' ? (entry.playUrl || entry.anilistId) : entry.id))
    .sort((a, b) => b.updatedAt - a.updatedAt);
  if (!entries.length || !$('results').classList.contains('home-results')) return;
  const rail = createRail('Riprendi', entries, { resume: true });
  rail.id = 'resume-rail';
  rail.dataset.resumeRail = 'true';
  $('results').prepend(rail);
}

function setupHomeMoreSentinel() {
  const old = $('home-more-sentinel');
  if (old) homeMoreObserver?.unobserve(old);
  old?.remove();
  if (elements.source.value !== 'sc' || !hasHomeMore()) return;
  const sentinel = el('div', 'home-more-sentinel');
  sentinel.id = 'home-more-sentinel';
  sentinel.setAttribute('role', 'status');
  sentinel.setAttribute('aria-live', 'polite');
  const spinner = el('span', 'home-more-spinner');
  spinner.setAttribute('aria-hidden', 'true');
  const label = el('span', 'home-more-label', 'Scopri altri titoli');
  sentinel.append(spinner, label);
  elements.results.append(sentinel);
  if (homeMoreFailed) showHomeMoreError();
  homeMoreObserver ||= new IntersectionObserver(entries => {
    if (entries.some(entry => entry.isIntersecting)) loadMoreHome();
  }, { rootMargin: '650px 0px', threshold: 0 });
  homeMoreObserver.observe(sentinel);
}

function hasHomeMore() {
  return Object.values(homeMoreCursors).some(cursor => Number.isInteger(cursor));
}

function showHomeMoreError() {
  const sentinel = $('home-more-sentinel');
  if (!sentinel) return;
  sentinel.classList.add('has-error');
  sentinel.querySelector('.home-more-label').textContent = 'Non riesco a caricare altri titoli.';
  let retry = sentinel.querySelector('button');
  if (!retry) {
    retry = el('button', 'home-more-retry', 'Riprova');
    retry.type = 'button';
    retry.addEventListener('click', () => {
      homeMoreFailed = false;
      loadMoreHome();
    });
    sentinel.append(retry);
  }
}

function addHomeItems(source, items) {
  const fresh = items.filter(item => {
    const key = `${item.kind}:${item.id || item.slug}`;
    if (homeLoadedIds.has(key)) return false;
    homeLoadedIds.add(key);
    return true;
  });
  if (!fresh.length) return 0;

  const addToSection = (title, item) => {
    let section = lastCatalogSections.find(row => row.title === title);
    let rail = [...elements.results.querySelectorAll('.catalog-rail-section')]
      .find(row => row.dataset.homeSection === title);
    if (!section) {
      section = { title, items: [] };
      lastCatalogSections.push(section);
    }
    if (!rail) {
      rail = createRail(title, []);
      rail.dataset.homeSection = title;
      elements.results.insertBefore(rail, $('home-more-sentinel'));
    }
    section.items.push(item);
    const cards = rail.querySelector('.catalog-rail');
    const card = makeCard(item);
    cards.append(card);
    observeCards(rail);
    const left = rail.querySelector('.rail-arrow-left');
    const right = rail.querySelector('.rail-arrow-right');
    updateRailButtons(cards, left, right);
  };
  const genreSections = [
    ['Drammi da scoprire', new Set(['drama'])],
    ['Crime e mistero', new Set(['crime', 'mystery'])],
    ['Commedie', new Set(['comedy'])],
    ['Fantasy e fantascienza', new Set(['fantasy', 'science-fiction'])],
    ['Azione e avventura', new Set(['action', 'adventure'])],
  ];

  for (const item of fresh) {
    if (source === 'movies') {
      addToSection('Film da scoprire', item);
      const year = Number(item.meta?.match(/\b\d{4}\b/)?.[0] || 0);
      if (year > 0 && year <= 2000) addToSection('Classici del cinema', item);
      continue;
    }
    addToSection('Serie TV popolari', item);
    const genres = new Set((item.genres || []).map(genre => String(genre).toLowerCase()));
    genreSections.forEach(([title, wanted]) => {
      if ([...wanted].some(genre => genres.has(genre))) addToSection(title, item);
    });
  }
  return fresh.length;
}

async function loadMoreHome() {
  if (homeMoreLoading || homeMoreFailed || !hasHomeMore()
    || !elements.results.classList.contains('home-results')) return;
  homeMoreLoading = true;
  const epoch = homeMoreEpoch;
  const sentinel = $('home-more-sentinel');
  sentinel?.classList.add('is-loading');
  sentinel?.classList.remove('has-error');
  if (sentinel) sentinel.querySelector('.home-more-label').textContent = 'Carico altri titoli…';
  const requests = Object.entries(homeMoreCursors)
    .filter(([, cursor]) => Number.isInteger(cursor))
    .map(async ([source, cursor]) => ({
      source,
      data: await api(`/api/home/more?source=${encodeURIComponent(source)}&cursor=${cursor}`),
    }));
  const results = await Promise.allSettled(requests);
  if (epoch !== homeMoreEpoch) return;
  let failed = false;
  for (const result of results) {
    if (result.status !== 'fulfilled') {
      failed = true;
      console.error('Caricamento dei titoli successivi non riuscito.', result.reason);
      continue;
    }
    const { source, data } = result.value;
    homeMoreCursors[source] = Number.isInteger(data.next_cursor) ? data.next_cursor : null;
    addHomeItems(source, filterItems(data.items || []));
  }
  homeMoreLoading = false;
  sentinel?.classList.remove('is-loading');
  if (failed) {
    homeMoreFailed = true;
    showHomeMoreError();
  } else if (hasHomeMore()) {
    if (sentinel) {
      sentinel.classList.remove('has-error');
      sentinel.querySelector('button')?.remove();
      sentinel.querySelector('.home-more-label').textContent = 'Scopri altri titoli';
    }
  } else {
    if (sentinel) homeMoreObserver?.unobserve(sentinel);
    sentinel?.remove();
    const end = el('div', 'home-more-end', 'Sei arrivato in fondo al catalogo.');
    elements.results.append(end);
  }
  const count = homeLoadedIds.size;
  elements.resultsCount.textContent = count ? `${count} titoli` : '';
  updatePageScrollbar();
}

function setupPageScrollbar() {
  const scrollbar = $('page-scrollbar');
  const thumb = scrollbar?.querySelector('.page-scrollbar-thumb');
  if (!scrollbar || !thumb) return;
  const update = () => {
    pageScrollFrame = 0;
    const documentHeight = Math.max(
      document.documentElement.scrollHeight,
      document.body.scrollHeight,
    );
    const maxScroll = Math.max(0, documentHeight - window.innerHeight);
    const trackHeight = scrollbar.clientHeight;
    if (maxScroll <= 1 || trackHeight <= 0) {
      scrollbar.classList.remove('is-scrollable');
      scrollbar.setAttribute('aria-valuemax', '0');
      scrollbar.setAttribute('aria-valuenow', '0');
      return;
    }
    const thumbHeight = Math.min(trackHeight, Math.max(42,
      trackHeight * window.innerHeight / documentHeight));
    const maxThumbTop = trackHeight - thumbHeight;
    const scrollTop = Math.min(maxScroll, Math.max(0,
      window.scrollY || document.documentElement.scrollTop));
    const thumbTop = maxScroll ? maxThumbTop * scrollTop / maxScroll : 0;
    scrollbar.classList.add('is-scrollable');
    thumb.style.height = `${thumbHeight}px`;
    thumb.style.transform = `translateY(${thumbTop}px)`;
    scrollbar.setAttribute('aria-valuemax', String(Math.round(maxScroll)));
    scrollbar.setAttribute('aria-valuenow', String(Math.round(scrollTop)));
    scrollbar.setAttribute('aria-valuetext', `${Math.round(scrollTop / maxScroll * 100)}%`);
  };
  const scheduleUpdate = () => {
    if (!pageScrollFrame) pageScrollFrame = requestAnimationFrame(update);
  };
  updatePageScrollbar = update;
  const markScrolling = () => {
    scrollbar.classList.add('is-scrolling');
    clearTimeout(pageScrollHideTimer);
    pageScrollHideTimer = setTimeout(() => scrollbar.classList.remove('is-scrolling'), 900);
    scheduleUpdate();
  };
  let drag = null;
  scrollbar.addEventListener('pointerdown', event => {
    const rect = scrollbar.getBoundingClientRect();
    const trackHeight = scrollbar.clientHeight;
    const thumbHeight = thumb.getBoundingClientRect().height;
    const maxThumbTop = Math.max(0, trackHeight - thumbHeight);
    const thumbTop = Number(thumb.style.transform.match(/translateY\(([-\d.]+)px\)/)?.[1] || 0);
    if (event.target === thumb) {
      drag = { startY: event.clientY, startTop: thumbTop, maxThumbTop };
      thumb.setPointerCapture(event.pointerId);
    } else {
      const desiredTop = Math.min(maxThumbTop, Math.max(0,
        event.clientY - rect.top - thumbHeight / 2));
      const maxScroll = document.documentElement.scrollHeight - window.innerHeight;
      document.documentElement.scrollTop = maxThumbTop ? desiredTop / maxThumbTop * maxScroll : 0;
      drag = { startY: event.clientY, startTop: desiredTop, maxThumbTop };
      scrollbar.setPointerCapture(event.pointerId);
    }
    event.preventDefault();
    markScrolling();
  });
  scrollbar.addEventListener('pointermove', event => {
    if (!drag) return;
    const top = Math.min(drag.maxThumbTop, Math.max(0,
      drag.startTop + event.clientY - drag.startY));
    const thumbHeight = thumb.getBoundingClientRect().height;
    const maxScroll = document.documentElement.scrollHeight - window.innerHeight;
    const maxThumbTop = scrollbar.clientHeight - thumbHeight;
    document.documentElement.scrollTop = maxThumbTop ? top / maxThumbTop * maxScroll : 0;
    markScrolling();
  });
  const stopDragging = () => { drag = null; };
  scrollbar.addEventListener('pointerup', stopDragging);
  scrollbar.addEventListener('pointercancel', stopDragging);
  scrollbar.addEventListener('keydown', event => {
    const maxScroll = Math.max(0,
      document.documentElement.scrollHeight - window.innerHeight);
    const current = window.scrollY || document.documentElement.scrollTop;
    const page = window.innerHeight * .85;
    const positions = {
      ArrowDown: current + 48,
      ArrowUp: current - 48,
      PageDown: current + page,
      PageUp: current - page,
      Home: 0,
      End: maxScroll,
    };
    if (!(event.key in positions)) return;
    event.preventDefault();
    window.scrollTo({ top: positions[event.key], behavior: 'smooth' });
    markScrolling();
  });
  window.addEventListener('scroll', markScrolling, { passive: true });
  window.addEventListener('resize', scheduleUpdate, { passive: true });
  if ('ResizeObserver' in window) {
    const resizeObserver = new ResizeObserver(scheduleUpdate);
    resizeObserver.observe(document.documentElement);
    resizeObserver.observe(document.body);
  }
  if ('MutationObserver' in window) {
    const mutationObserver = new MutationObserver(scheduleUpdate);
    mutationObserver.observe(document.body, { childList: true, subtree: true });
  }
  update();
}

function closeSidebar() {
  document.body.classList.remove('sidebar-open', 'sidebar-hover');
  $('sidebar-toggle').setAttribute('aria-expanded', 'false');
  elements.query.blur();
}

async function loadHome({ syncRoute = true } = {}) {
  showingFavorites = false;
  homeMoreEpoch += 1;
  homeMoreLoading = false;
  homeMoreCursors = { sc: null, movies: null };
  homeMoreFailed = false;
  homeLoadedIds.clear();
  showCatalog();
  const requestId = ++catalogRequest;
  searchController?.abort();
  elements.query.value = '';
  elements.results.replaceChildren();
  elements.results.classList.remove('home-results');
  elements.resultsCount.textContent = '';
  elements.catalogHero.classList.add('hidden');
  lastCatalogHash = `#/catalog/${elements.source.value}`;
  if (syncRoute) history.replaceState(null, '', lastCatalogHash);
  showStatus('Carico il catalogo…');
  try {
    const data = await api(elements.source.value === 'au' ? '/api/anime/home' : '/api/home');
    if (requestId === catalogRequest) renderHome(data);
  } catch (error) {
    if (requestId === catalogRequest) {
      showRetryStatus(`Catalogo non disponibile: ${error.message}`, loadHome);
    }
  }
}

async function runSearch(query, { syncRoute = true } = {}) {
  const normalizedQuery = query.trim();
  showSearchScreen();
  if (normalizedQuery.length < 2) {
    elements.searchResults.replaceChildren();
    elements.searchResultsCount.textContent = '';
    elements.searchTitle.textContent = 'I risultati appariranno qui';
    showSearchStatus('Inserisci almeno 2 caratteri per cercare.');
    return;
  }
  const requestId = ++catalogRequest;
  searchController?.abort();
  searchController = new AbortController();
  const source = elements.source.value;
  const kind = source === 'au' ? 'anime' : 'serie';
  const cacheKey = `${source}:${elements.kind.value}:${normalizedQuery.toLowerCase()}`;
  if (syncRoute) {
    const params = new URLSearchParams({ q: normalizedQuery, kind: elements.kind.value });
    lastCatalogHash = `#/search/${source}?${params}`;
    history.replaceState(null, '', lastCatalogHash);
  }
  elements.searchResults.replaceChildren();
  elements.searchResultsCount.textContent = '';
  elements.searchTitle.textContent = `Cerco “${normalizedQuery}”…`;
  showSearchStatus('Sto cercando nel catalogo…');
  try {
    const data = await withLoading('Sto cercando...', async () => {
      if (searchCache.has(cacheKey)) return searchCache.get(cacheKey);
      const params = new URLSearchParams({
        q: normalizedQuery,
        kind,
        media_type: source === 'sc' ? elements.kind.value : 'all',
      });
      const result = await api(`/api/search?${params}`, searchController.signal);
      const items = filterItems(result.items);
      const searchResults = { items, warnings: result.warnings || [] };
      searchCache.set(cacheKey, searchResults);
      return searchResults;
    });
    if (requestId !== catalogRequest) return;
    renderResults(data.items, `Risultati per “${normalizedQuery}”`);
    if (data.warnings?.length) showSearchStatus(data.warnings.join(' '), true);
    else if (!data.items.length) showSearchStatus('Nessun risultato trovato.');
  } catch (error) {
    if (error.name === 'AbortError' || requestId !== catalogRequest) return;
    showSearchStatus(`Ricerca non riuscita: ${error.message}`, true,
      () => runSearch(normalizedQuery, { syncRoute: false }));
  }
}

function renderTags(values, className) {
  const container = el('div', className);
  (values || []).forEach(value => container.append(el('span', 'detail-tag', value)));
  return container;
}

function isAnimeMovie(data) {
  return data.format === 'MOVIE' || /\b(movie|film|gekijouban)\b/i.test(data.title || '');
}

function renderAnimeEpisodeAvailability(data) {
  if (data.episodes?.length) {
    elements.episodes.classList.remove('hidden');
    return;
  }
  if (isAnimeMovie(data)) {
    elements.episodes.classList.add('hidden');
    return;
  }
  elements.episodesTitle.textContent = `Episodi di ${data.title}`;
  elements.episodesList.replaceChildren(el('p', 'episodes-empty',
    data.episodes_error || 'Nessun episodio disponibile.'));
  elements.episodes.classList.remove('hidden');
}

function makeEpisodeRow(episode, onPlay, fallbackPoster = '') {
  const number = episode.number ?? '?';
  const button = el('button', 'episode-row');
  button.type = 'button';
  button.setAttribute('aria-label', `Riproduci episodio ${number}: ${episode.label || `Episodio ${number}`}`);
  const posterUrl = episode.poster || fallbackPoster;
  if (posterUrl) {
    const poster = el('img', 'episode-poster');
    poster.alt = '';
    poster.loading = 'lazy';
    poster.src = posterUrl;
    useImageFallback(poster, posterUrl);
    button.append(poster);
  } else {
    button.append(el('span', 'episode-poster episode-poster-placeholder', '▶'));
  }
  const copy = el('span', 'episode-copy');
  const heading = el('span', 'episode-row-heading');
  heading.append(
    el('span', 'episode-number', `EP ${String(number).padStart(2, '0')}`),
    el('strong', 'episode-name', episode.label || `Episodio ${number}`),
  );
  copy.append(heading);
  const metadata = [episode.duration, episode.meta].filter(Boolean).join(' · ');
  if (metadata) copy.append(el('span', 'episode-meta', metadata));
  button.append(copy, el('span', 'episode-play-icon', '▶'));
  button.addEventListener('click', onPlay);
  return button;
}

function renderAnimeDetail(data) {
  const displayTitle = data.display_title || data.title;
  $('detail-title').textContent = displayTitle || 'Anime';
  $('detail-plot').textContent = data.plot || 'Trama non disponibile.';
  $('detail-tags').replaceChildren(...(data.badges || []).map(badge => el('span', 'detail-tag', badge)));
  const metadata = [el('span', '', 'Anime')];
  if (data.season_year) metadata.push(el('span', '', String(data.season_year)));
  if (data.rating) metadata.push(el('span', 'detail-rating', `★ ${data.rating} AniList`));
  $('detail-metadata').replaceChildren(...metadata);
  $('detail-genres').replaceChildren(...(data.genres || []).map(genre => el('span', 'detail-tag', genre)));
  $('detail-cast').classList.add('hidden');
  const attribution = ({
    Wikipedia: 'Metadati anime da AniList · titoli episodi da Wikipedia',
    MyAnimeList: 'Metadati anime da AniList · episodi e titoli da MyAnimeList',
    'Wikipedia e MyAnimeList': 'Metadati anime da AniList · episodi da Wikipedia e MyAnimeList',
  })[data.episodes_source] || 'Metadati ed episodi da AniList';
  $('detail-attribution').textContent = `${attribution}${translationNotice(data.translation_status)}`;
  setDetailImages(data.poster);
  const imdbLink = $('detail-imdb');
  imdbLink.href = `https://www.imdb.com/find/?q=${encodeURIComponent(data.title || '')}`;
  imdbLink.replaceChildren(document.createTextNode('Apri su IMDb '), el('span', '', '↗'));
  imdbLink.classList.remove('hidden');
  if (data.site_url) {
    $('detail-anilist').href = data.site_url;
    $('detail-anilist').classList.remove('hidden');
  } else $('detail-anilist').classList.add('hidden');
  if (data.mal_id) {
    $('detail-mal').href = `https://myanimelist.net/anime/${encodeURIComponent(data.mal_id)}`;
    $('detail-mal').classList.remove('hidden');
  } else $('detail-mal').classList.add('hidden');
  $('detail-watch').querySelector('span').textContent = isAnimeMovie(data) ? 'Guarda il film' : 'Guarda';
  elements.episodesTitle.textContent = `Episodi di ${displayTitle}`;
  elements.episodesList.replaceChildren();
  if (isAnimeMovie(data)) {
    elements.episodes.classList.add('hidden');
  } else if (!data.episodes?.length) {
    renderAnimeEpisodeAvailability(data);
  } else {
    elements.episodes.classList.remove('hidden');
    elements.episodesTitle.textContent = `Episodi di ${displayTitle} · ${data.episodes.length}`;
    data.episodes.forEach(episode => {
      const button = makeEpisodeRow(episode, () =>
        openAnime('', episodePlaybackTitle(data.title, episode.label || `Episodio ${episode.number}`), {
          episode: episode.number || 1,
          anilistId: data.anilist_id,
          triggerButton: button,
        }), data.poster);
      elements.episodesList.append(button);
    });
  }
}

function renderSerieDetail(data) {
  const isMovie = data.kind === 'movie' || data.catalog_source === 'wikidata';
  const displayTitle = data.display_title || data.title;
  $('detail-title').textContent = displayTitle || 'Titolo';
  $('detail-plot').textContent = data.plot || 'Trama non disponibile.';
  $('detail-tags').replaceChildren(...(data.seasons?.length
    ? [el('span', 'detail-tag', `${data.seasons.length} stagioni`)] : []));
  $('detail-metadata').replaceChildren(el('span', '', isMovie ? 'Film' : 'Serie TV'));
  $('detail-genres').replaceChildren(...(data.genres || []).map(genre => el('span', 'detail-tag', genre)));
  $('detail-cast').classList.add('hidden');
  const attribution = data.catalog_source === 'tvmaze'
    ? 'Metadati ed episodi da TVmaze'
    : 'Metadati da Wikidata';
  $('detail-attribution').textContent = `${attribution}${translationNotice(data.translation_status)}`;
  setDetailImages(data.poster);
  const sourceLink = $('detail-imdb');
  sourceLink.href = data.site_url || `https://www.wikidata.org/wiki/${encodeURIComponent(data.id || '')}`;
  sourceLink.replaceChildren(
    document.createTextNode(data.catalog_source === 'tvmaze' ? 'TVmaze ' : 'Wikidata '),
    el('span', '', '↗'),
  );
  sourceLink.classList.remove('hidden');
  $('detail-anilist').classList.add('hidden');
  $('detail-mal').classList.add('hidden');
  $('detail-watch').querySelector('span').textContent = isMovie ? 'Guarda il film' : 'Guarda';
  elements.episodesTitle.textContent = displayTitle || 'Episodi';
  elements.episodesList.replaceChildren();
  if (isMovie || !data.seasons?.length) {
    elements.episodes.classList.add('hidden');
    return;
  }
  elements.episodes.classList.remove('hidden');
  const seasons = data.seasons;
  const seasonSelect = document.createElement('select');
  seasonSelect.className = 'episode-season';
  seasons.forEach((season, index) => {
    const option = document.createElement('option');
    option.value = String(Number(season.match(/\d+/)?.[0]) || index + 1);
    option.textContent = season;
    seasonSelect.append(option);
  });
  elements.episodesList.append(seasonSelect);
  enhanceSelect(seasonSelect);
  const episodeButtons = el('div', 'episode-list-items');
  elements.episodesList.append(episodeButtons);
  const renderSeason = seasonNumber => {
    const episodes = data.episodes?.[String(seasonNumber)] || [];
    episodeButtons.replaceChildren();
    const seasonName = seasons.find((name, index) =>
      String(Number(name.match(/\d+/)?.[0]) || index + 1) === String(seasonNumber)) || `Stagione ${seasonNumber}`;
    elements.episodesTitle.textContent = `${seasonName} · ${episodes.length} episodi`;
    if (!episodes.length) {
      episodeButtons.append(el('p', 'episodes-empty', 'Nessun episodio disponibile.'));
      return;
    }
    episodes.forEach(episode => {
      const button = makeEpisodeRow(episode, () =>
        openSerie(
          data.id,
          Number(seasonNumber),
          Number(episode.number),
          episodePlaybackTitle(data.title, episode.label || `Episodio ${episode.number}`),
          { triggerButton: button },
        ), data.poster);
      episodeButtons.append(button);
    });
  };
  seasonSelect.addEventListener('change', () => renderSeason(seasonSelect.value));
  renderSeason(seasonSelect.value || '1');
}

function translationNotice(status) {
  if (status === 'failed') return ' · Traduzione non disponibile; testo originale mostrato';
  if (status === 'not_configured') return ' · Traduzione non configurata';
  return '';
}

function episodePlaybackTitle(seriesTitle, episodeTitle) {
  const normalize = value => String(value || '').normalize('NFKD')
    .replace(/[\u0300-\u036f]/g, '').toLocaleLowerCase().replace(/[^a-z0-9]+/g, ' ').trim();
  return normalize(seriesTitle) === normalize(episodeTitle)
    ? seriesTitle
    : `${seriesTitle} — ${episodeTitle}`;
}

function setDetailImages(posterUrl) {
  const poster = $('detail-poster');
  const backdrop = $('detail-backdrop');
  poster.classList.toggle('hidden', !posterUrl);
  backdrop.classList.toggle('hidden', !posterUrl);
  if (posterUrl) {
    poster.src = posterUrl;
    backdrop.src = posterUrl;
    useImageFallback(poster, posterUrl);
    useImageFallback(backdrop, posterUrl);
  } else {
    poster.removeAttribute('src');
    backdrop.removeAttribute('src');
  }
}

async function openDetail(type, id, slug = '') {
  let anilistId = '';
  if (type === 'anime' && id.startsWith('anilist-')) anilistId = id.slice('anilist-'.length);
  showCatalog();
  currentDetail = null;
  elements.catalogSection.classList.add('hidden');
  elements.detail.classList.remove('hidden');
  elements.episodes.classList.add('hidden');
  $('related-panel').classList.add('hidden');
  $('related-list').replaceChildren();
  $('detail-title').textContent = 'Caricamento…';
  $('detail-plot').textContent = '';
  elements.detail.scrollIntoView({ behavior: 'smooth', block: 'start' });
  try {
    const data = await withLoading(type === 'anime'
      ? 'Sto caricando i metadati da AniList...'
      : 'Sto caricando i dettagli...', async () => {
      return api(type === 'anime'
        ? `/api/anime/${encodeURIComponent(id)}?anilist_id=${encodeURIComponent(anilistId)}`
        : `/api/serie/${encodeURIComponent(id)}?slug=${encodeURIComponent(slug)}`);
    });
    currentDetail = { type, data };
    clearStatus();
    if (type === 'anime') renderAnimeDetail(data);
    else renderSerieDetail(data);
    loadRelatedTitles(data, type, anilistId, slug);
    elements.detail.scrollIntoView({ behavior: 'smooth', block: 'start' });
  } catch (error) {
    $('detail-title').textContent = 'Dettagli non disponibili';
    $('detail-plot').textContent = error.message;
  }
}

async function loadRelatedTitles(data, type, anilistId = '', lookupTitle = '') {
  try {
    const params = {
      title: lookupTitle || data.title || '',
      kind: type,
    };
    const relatedId = anilistId || data.anilist_id;
    if (relatedId) params.anilist_id = relatedId;
    const result = await api(`/api/related?${new URLSearchParams(params)}`);
    if (currentDetail?.data !== data || !result.sections?.length) return;
    const cards = result.sections.flatMap(section => section.items || []);
    if (!cards.length) return;
    $('related-panel').classList.remove('hidden');
    $('related-list').replaceChildren(...result.sections.map(section => {
      const rail = createRail(section.title, section.items || []);
      rail.classList.add('related-rail');
      return rail;
    }));
  } catch (error) {
    console.info('Contenuti correlati non disponibili.', error);
  }
}

function destroyHls() {
  clearTimeout(hlsRecoveryTimer);
  hlsRecoveryTimer = null;
  if (hls) {
    hls.destroy();
    hls = null;
  }
}

function attachVideo(url) {
  destroyHls();
  elements.video.pause();
  elements.video.removeAttribute('src');
  elements.video.load();
  elements.player.classList.remove('is-ready', 'is-error');
  elements.player.classList.add('is-resolving');
  elements.playerLoading.classList.remove('hidden');
  elements.playerStatus.textContent = 'Connessione allo stream…';
  hlsRecoveryAttempts = { network: 0, media: 0 };
  if (resumeSeekTime > 0) {
    elements.video.addEventListener('loadedmetadata', () => {
      if (resumeSeekTime < elements.video.duration - 1) {
        elements.video.currentTime = resumeSeekTime;
      }
      resumeSeekTime = 0;
    }, { once: true });
  }
  if (url.includes('.m3u8') && window.Hls && window.Hls.isSupported()) {
    hls = new window.Hls({
      maxBufferLength: 30,
      manifestLoadingMaxRetry: 5,
      levelLoadingMaxRetry: 5,
      fragLoadingMaxRetry: 6,
      manifestLoadingRetryDelay: 1000,
      levelLoadingRetryDelay: 1000,
      fragLoadingRetryDelay: 1000,
      fragLoadingMaxRetryTimeout: 20000,
    });
    const activeHls = hls;
    activeHls.on(window.Hls.Events.AUDIO_TRACKS_UPDATED, (_event, data) => {
      const tracks = data.audioTracks || activeHls.audioTracks || [];
      const preference = preferredAudio();
      const languageTrack = track => `${track.lang || ''} ${track.name || ''}`.toLowerCase();
      const italian = tracks.find(track => /\b(?:it|ita|italian|italiano)\b/.test(languageTrack(track)));
      const japanese = tracks.find(track => /\b(?:ja|jpn|japanese|giapponese)\b/.test(languageTrack(track)));
      let selected;
      if (preference === 'it') {
        selected = italian || (resumeContext?.kind === 'anime' ? japanese : null);
      } else if (preference === 'original' && resumeContext?.kind === 'anime') {
        selected = japanese;
      } else if (preference === 'original') {
        selected = tracks.find(track => !/\b(?:it|ita|italian|italiano)\b/.test(languageTrack(track)));
      }
      selected ||= tracks[0];
      if (selected && activeHls.audioTrack !== selected.id) activeHls.audioTrack = selected.id;
    });
    hls.on(window.Hls.Events.ERROR, (_event, data) => {
      if (data.fatal) {
        if (data.type === window.Hls.ErrorTypes.NETWORK_ERROR
          && hlsRecoveryAttempts.network < 3) {
          hlsRecoveryAttempts.network += 1;
          const attempt = hlsRecoveryAttempts.network;
          elements.playerStatus.textContent = `Riconnessione allo stream (${attempt}/3)…`;
          hlsRecoveryTimer = setTimeout(() => {
            if (hls === activeHls) activeHls.startLoad();
          }, attempt * 900);
          return;
        }
        if (data.type === window.Hls.ErrorTypes.MEDIA_ERROR
          && hlsRecoveryAttempts.media < 2) {
          hlsRecoveryAttempts.media += 1;
          elements.playerStatus.textContent = 'Ripristino della riproduzione…';
          activeHls.recoverMediaError();
          return;
        }
        const message = `Errore stream: ${data.details}`;
        setPlayerError(message);
        elements.video.dispatchEvent(new CustomEvent('playerloaderror', { detail: message }));
      }
    });
    hls.loadSource(url);
    hls.attachMedia(elements.video);
  } else {
    elements.video.src = url;
  }
}

function waitForVideoReady(signal) {
  return new Promise((resolve, reject) => {
    let timeout;
    const cleanup = () => {
      clearTimeout(timeout);
      elements.video.removeEventListener('loadeddata', onReady);
      elements.video.removeEventListener('playerloaderror', onError);
      elements.video.removeEventListener('error', onMediaError);
      signal.removeEventListener('abort', onAbort);
    };
    const onReady = () => {
      cleanup();
      resolve();
    };
    const onError = event => {
      cleanup();
      reject(new Error(event.detail || 'Lo stream non è disponibile.'));
    };
    const onMediaError = () => {
      cleanup();
      reject(new Error('Il video non è disponibile o non può essere riprodotto.'));
    };
    const onAbort = () => {
      cleanup();
      reject(new DOMException('Riproduzione annullata.', 'AbortError'));
    };
    if (signal.aborted) {
      onAbort();
      return;
    }
    elements.video.addEventListener('loadeddata', onReady, { once: true });
    elements.video.addEventListener('playerloaderror', onError, { once: true });
    elements.video.addEventListener('error', onMediaError, { once: true });
    signal.addEventListener('abort', onAbort, { once: true });
    timeout = setTimeout(() => {
      cleanup();
      reject(new Error('Lo stream impiega troppo tempo a caricarsi.'));
    }, 45000);
    if (elements.video.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA) onReady();
  });
}

async function prepareVideo(url, signal) {
  elements.player.classList.remove('hidden', 'is-ready', 'is-error');
  elements.player.classList.add('is-preloading', 'is-resolving');
  elements.playerLoading.classList.remove('hidden');
  elements.playerStatus.textContent = 'Caricamento del video…';
  const ready = waitForVideoReady(signal);
  attachVideo(url);
  await ready;
  if (signal.aborted) throw new DOMException('Riproduzione annullata.', 'AbortError');
}

function startPreparedVideo() {
  setPlayerReady();
  elements.video.play().catch(error => {
    elements.playerStatus.textContent = 'Video pronto. Premi play per avviare.';
    elements.player.classList.add('is-autoplay-blocked');
    elements.playerLoading.classList.remove('hidden');
    console.info('Riproduzione automatica non consentita; il video è pronto per il play manuale.', error);
  });
}

function setPlayerReady() {
  if (elements.player.classList.contains('is-preloading')) return;
  elements.player.classList.remove('is-resolving', 'is-error', 'is-autoplay-blocked');
  elements.player.classList.add('is-ready');
  elements.playerLoading.classList.add('hidden');
  $('player-retry').classList.add('hidden');
}

function setPlayerError(message) {
  elements.player.classList.remove('is-resolving', 'is-ready');
  elements.player.classList.add('is-error');
  elements.playerLoading.classList.remove('hidden');
  elements.playerStatus.textContent = message;
  elements.sourceInfo.textContent = message;
  $('player-retry').classList.remove('hidden');
}

function retryCurrentPlayback() {
  const entry = resumeContext;
  if (!entry) return;
  if (entry.kind === 'anime' && entry.playUrl) {
    openAnime(entry.playUrl, entry.title, { resumeEntry: entry, resumeTime: elements.video.currentTime || 0 });
  } else if (entry.kind === 'serie' && entry.id) {
    openSerie(entry.id, entry.season || 1, entry.episode || 1, entry.title, {
      resumeEntry: entry,
      resumeTime: elements.video.currentTime || 0,
    });
  }
}

function formatPlayerTime(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return '0:00';
  const total = Math.floor(seconds);
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor(total / 60) % 60;
  const remainder = total % 60;
  return hours
    ? `${hours}:${String(minutes).padStart(2, '0')}:${String(remainder).padStart(2, '0')}`
    : `${minutes}:${String(remainder).padStart(2, '0')}`;
}

function updatePlayerControls() {
  const { duration, currentTime } = elements.video;
  const progress = Number.isFinite(duration) && duration > 0 ? currentTime / duration * 100 : 0;
  elements.playerSeek.value = String(Math.round(progress * 10));
  elements.playerSeek.style.setProperty('--seek-progress', `${progress}%`);
  elements.playerCurrentTime.textContent = formatPlayerTime(currentTime);
  elements.playerDuration.textContent = formatPlayerTime(duration);
  const paused = elements.video.paused;
  elements.playerPlay.classList.toggle('is-playing', !paused);
  elements.playerPlay.setAttribute('aria-label', paused ? 'Riproduci' : 'Pausa');
  elements.player.classList.toggle('player-paused', paused);
  elements.playerCenterPlay.setAttribute('aria-label', paused ? 'Riproduci' : 'Pausa');
  if (paused) {
    clearTimeout(playerControlsTimer);
    elements.player.classList.remove('player-controls-hidden');
  }
}

function revealPlayerControls() {
  elements.player.classList.remove('player-controls-hidden');
  clearTimeout(playerControlsTimer);
  if (!elements.video.paused) {
    playerControlsTimer = setTimeout(() => {
      if (!elements.video.paused) elements.player.classList.add('player-controls-hidden');
    }, 2800);
  }
}

function seekBy(seconds) {
  const duration = elements.video.duration;
  if (!Number.isFinite(duration) || duration <= 0) return;
  elements.video.currentTime = Math.max(0, Math.min(duration, elements.video.currentTime + seconds));
  revealPlayerControls();
}

function updatePlayerQualities(variants, sourceUrl) {
  const unique = new Map();
  (variants || [])
    .filter(variant => variant.url && Number(variant.height) > 0)
    .sort((a, b) => b.height - a.height)
    .forEach(variant => {
      if (!unique.has(variant.url)) unique.set(variant.url, variant);
    });
  const ordered = [...unique.values()];
  const defaultVariant = ordered.find(variant => variant.url === sourceUrl) || ordered[0];
  const masterPlaylist = /\.m3u8(?:$|[?#])/i.test(sourceUrl)
    && ordered.length > 0
    && !ordered.some(variant => variant.url === sourceUrl);
  playerQualitySources = masterPlaylist
    ? [{ url: sourceUrl, height: 0, automatic: true }, ...ordered]
    : defaultVariant
    ? [defaultVariant, ...ordered.filter(variant => variant !== defaultVariant)]
    : [{ url: sourceUrl, height: ordered[0]?.height || 0 }, ...ordered];
  elements.playerQuality.replaceChildren();
  playerQualitySources.forEach((variant, index) => {
    const option = document.createElement('option');
    option.value = String(index);
    option.textContent = variant.automatic
      ? 'Automatica · Adattiva'
      : `${variant.height ? `${variant.height}p` : 'Automatica'}${index === 0 ? ' · Massima' : ''}`;
    elements.playerQuality.append(option);
  });
  elements.playerQuality.disabled = playerQualitySources.length < 2;
  enhanceSelect(elements.playerQuality);
}

function selectPlayerQuality() {
  const variant = playerQualitySources[Number(elements.playerQuality.value)];
  if (!variant) return;
  if (hls && (variant.automatic || variant.height)) {
    if (variant.automatic) {
      hls.currentLevel = -1;
      elements.sourceInfo.textContent = 'Qualità adattiva: selezione automatica.';
    } else {
      const level = hls.levels.findIndex(item => Number(item.height) === Number(variant.height));
      if (level < 0) {
        elements.sourceInfo.textContent = `La qualità ${variant.height}p non è disponibile in questo stream.`;
        return;
      }
      hls.currentLevel = level;
      elements.sourceInfo.textContent = `Qualità selezionata: ${variant.height}p`;
    }
    return;
  }
  if (variant.url === currentPlaybackSource) return;
  resumeSeekTime = elements.video.currentTime;
  currentPlaybackSource = variant.url;
  currentPlaybackUrl = proxied(variant.url, currentPlaybackReferer);
  currentPlaybackMp4 = /\.mp4(?:$|[?#])/i.test(variant.url) ? currentPlaybackUrl : '';
  elements.sourceInfo.textContent = `Qualità selezionata: ${variant.height}p\nSorgente: ${variant.url}`;
  attachVideo(currentPlaybackUrl);
  elements.video.play().catch(error => {
    elements.playerStatus.textContent = 'Qualità caricata. Premi play per continuare.';
    console.info('Riproduzione automatica della nuova qualità non consentita.', error);
  });
}

function enterPlayerFullscreen() {
  if (!elements.player.requestFullscreen || document.fullscreenElement === elements.player) return;
  elements.player.requestFullscreen().catch(error => {
    console.info('Schermo intero del browser non disponibile; resta attiva la modalità cinema.', error);
  });
}

function togglePlayerFullscreen() {
  if (document.fullscreenElement === elements.player) {
    document.exitFullscreen().catch(error => {
      console.error('Impossibile uscire dallo schermo intero.', error);
    });
  } else {
    enterPlayerFullscreen();
  }
}

function resumePlayback(entry) {
  if (entry.kind === 'anime') {
    openAnime(entry.playUrl, entry.title, {
      anilistId: entry.anilistId,
      episode: entry.episode || 1,
      resumeEntry: entry,
      resumeTime: entry.currentTime || 0,
    });
  } else {
    openSerie(entry.id, entry.season, entry.episode, entry.title, {
      resumeEntry: entry,
      resumeTime: entry.currentTime || 0,
    });
  }
}

function createResumeContext(kind, title, { episode = 1, season = 1, resumeEntry } = {}) {
  if (resumeEntry) return { ...resumeEntry, title };
  const data = currentDetail?.data || {};
  const episodeTitle = title.split(' — ')[1];
  return {
    kind,
    id: kind === 'anime' ? (data.slug || title) : data.id,
    slug: kind === 'anime' ? data.slug : data.slug,
    season,
    episode,
    title,
    item: {
      kind,
      id: data.id,
      slug: data.slug,
      title: data.title || title,
      poster: data.poster || '',
      meta: episodeTitle || (kind === 'anime' ? 'Anime' : `Stagione ${season} · Episodio ${episode}`),
    },
    playUrl: '',
    anilistId: kind === 'anime' ? data.anilist_id : undefined,
    progress: 0,
    currentTime: 0,
    updatedAt: Date.now(),
  };
}

function openPlayer(title) {
  const [seriesTitle, ...episodeParts] = title.split(' — ');
  elements.playingTitle.textContent = episodeParts.length
    ? episodePlaybackTitle(seriesTitle, episodeParts.join(' — '))
    : title;
  elements.playerStatus.textContent = 'Sto preparando la riproduzione…';
  elements.player.classList.remove('hidden', 'is-preloading', 'is-ready', 'is-error');
  elements.player.classList.add('is-resolving');
  elements.playerLoading.classList.remove('hidden');
  document.body.classList.add('player-open');
  revealPlayerControls();
  updatePlayerControls();
  enterPlayerFullscreen();
}

function closePlayer() {
  playbackController?.abort();
  playbackController = null;
  persistResumeProgress(true);
  destroyHls();
  elements.video.pause();
  elements.video.removeAttribute('src');
  elements.video.load();
  elements.player.classList.add('hidden');
  elements.player.classList.remove('is-preloading', 'is-ready', 'is-error', 'is-resolving');
  document.body.classList.remove('player-open');
  elements.player.classList.remove('player-controls-hidden', 'player-paused');
  clearTimeout(playerControlsTimer);
  if (document.fullscreenElement === elements.player) {
    document.exitFullscreen().catch(error => {
      console.error('Impossibile uscire dallo schermo intero.', error);
    });
  }
  currentPlaybackUrl = '';
  currentPlaybackMp4 = '';
  currentPlaybackSource = '';
  currentPlaybackReferer = '';
  currentPlaybackVariants = [];
  playerQualitySources = [];
  resumeContext = null;
  elements.playingTitle.textContent = '';
}

async function copyText(value, successMessage) {
  if (!value) {
    elements.sourceInfo.textContent = 'URL non disponibile.';
    return;
  }
  try {
    await navigator.clipboard.writeText(value);
    elements.sourceInfo.textContent = successMessage;
  } catch (error) {
    elements.sourceInfo.textContent = `Impossibile copiare l'URL: ${error.message}`;
  }
}

function mpvCommand(url) {
  return `mpv "${url.replace(/"/g, '\\"')}"`;
}

function wirePlayerActions() {
  $('player-retry').addEventListener('click', retryCurrentPlayback);
  $('copy-url').addEventListener('click', () => copyText(currentPlaybackUrl, 'URL copiato.'));
  $('copy-mp4').addEventListener('click', () =>
    copyText(currentPlaybackMp4, currentPlaybackMp4 ? 'URL MP4 copiato.' : 'URL MP4 non disponibile per questa sorgente.'));
  $('open-mp4').addEventListener('click', () => {
    if (currentPlaybackMp4) window.open(currentPlaybackMp4, '_blank', 'noopener,noreferrer');
    else elements.sourceInfo.textContent = 'File MP4 non disponibile per questa sorgente.';
  });
  $('download-mp4').addEventListener('click', () => {
    if (!currentPlaybackMp4) {
      elements.sourceInfo.textContent = 'File MP4 non disponibile per questa sorgente.';
      return;
    }
    const link = document.createElement('a');
    link.href = currentPlaybackMp4;
    link.download = '';
    link.click();
  });
  $('copy-mpv').addEventListener('click', () => copyText(
    currentPlaybackUrl ? mpvCommand(currentPlaybackUrl) : '', 'Comando mpv copiato.'));
  $('close-player').addEventListener('click', closePlayer);
  $('player-fullscreen').addEventListener('click', togglePlayerFullscreen);
  $('player-play').addEventListener('click', () => {
    revealPlayerControls();
    if (elements.video.paused) {
      elements.video.play().catch(error => {
        setPlayerError(`Impossibile avviare il video: ${error.message}`);
      });
    } else {
      elements.video.pause();
    }
  });
  elements.playerCenterPlay.addEventListener('click', () => $('player-play').click());
  $('player-skip-back').addEventListener('click', () => seekBy(-10));
  $('player-skip-forward').addEventListener('click', () => seekBy(10));
  // Scrubbing should only issue one range request when the user releases the
  // slider. Updating currentTime on every pointer movement can overwhelm hosts.
  elements.playerSeek.addEventListener('change', () => {
    const duration = elements.video.duration;
    if (Number.isFinite(duration) && duration > 0) {
      pendingSeekTime = duration * Number(elements.playerSeek.value) / 1000;
      elements.video.currentTime = pendingSeekTime;
    }
  });
  elements.playerVolume.addEventListener('input', () => {
    elements.video.volume = Number(elements.playerVolume.value);
    elements.video.muted = elements.video.volume === 0;
  });
  elements.playerQuality.addEventListener('change', selectPlayerQuality);
  elements.video.addEventListener('loadeddata', setPlayerReady);
  elements.video.addEventListener('playing', setPlayerReady);
  elements.video.addEventListener('timeupdate', updatePlayerControls);
  elements.video.addEventListener('durationchange', updatePlayerControls);
  elements.video.addEventListener('play', updatePlayerControls);
  elements.video.addEventListener('pause', updatePlayerControls);
  elements.video.addEventListener('volumechange', () => {
    elements.playerVolume.value = String(elements.video.muted ? 0 : elements.video.volume);
  });
  elements.video.addEventListener('seeked', () => { pendingSeekTime = null; });
  elements.video.addEventListener('error', () => {
    if (elements.player.classList.contains('hidden') || hls || !currentPlaybackUrl) return;
    if (nativeRecoveryAttempts < 2) {
      nativeRecoveryAttempts += 1;
      const target = pendingSeekTime ?? elements.video.currentTime;
      resumeSeekTime = target;
      elements.playerStatus.textContent = `Riconnessione al video (${nativeRecoveryAttempts}/2)…`;
      elements.playerLoading.classList.remove('hidden');
      setTimeout(() => {
        if (elements.player.classList.contains('hidden') || hls) return;
        elements.video.addEventListener('loadeddata', () => {
          elements.video.play().catch(error => {
            setPlayerError(`Impossibile riprendere il video: ${error.message}`);
          });
        }, { once: true });
        attachVideo(currentPlaybackUrl);
      }, 700 * nativeRecoveryAttempts);
      return;
    }
    setPlayerError('Connessione al video interrotta. Premi “Riprova a connetterti”.');
  });
  elements.video.addEventListener('playing', () => {
    nativeRecoveryAttempts = 0;
    hlsRecoveryAttempts = { network: 0, media: 0 };
    revealPlayerControls();
  });
  ['pointermove', 'pointerdown', 'touchstart'].forEach(eventName => {
    elements.player.addEventListener(eventName, revealPlayerControls, { passive: true });
  });
}

function showSourceResult(data, sourceUrl, referer, label) {
  const videoUrl = proxied(sourceUrl, referer);
  const mp4Url = /\.mp4(?:$|[?#])/i.test(sourceUrl) ? videoUrl : '';
  currentPlaybackSource = sourceUrl;
  currentPlaybackReferer = referer;
  currentPlaybackVariants = data.variants || [];
  currentPlaybackUrl = videoUrl;
  currentPlaybackMp4 = mp4Url;
  elements.sourceInfo.textContent = `${label}\nSorgente: ${sourceUrl}`;
  updatePlayerQualities(currentPlaybackVariants, sourceUrl);
  return videoUrl;
}

function setPlaybackButtonBusy(button) {
  const targets = new Set([button, $('detail-watch')].filter(Boolean));
  const original = [...targets].map(target => ({
    target,
    text: target.querySelector('span')?.textContent ?? target.textContent,
    disabled: target.disabled,
  }));
  targets.forEach(target => {
    target.disabled = true;
    const label = target.querySelector('span');
    if (label) label.textContent = 'Caricamento…';
    else target.textContent = 'Caricamento…';
  });
  return () => original.forEach(({ target, text, disabled }) => {
    target.disabled = disabled;
    const label = target.querySelector('span');
    if (label) label.textContent = text;
    else target.textContent = text;
  });
}

async function openAnime(url, title, options = {}) {
  persistResumeProgress(true);
  playbackController?.abort();
  nativeRecoveryAttempts = 0;
  hlsRecoveryAttempts = { network: 0, media: 0 };
  const controller = new AbortController();
  playbackController = controller;
  const restoreButton = setPlaybackButtonBusy(options.triggerButton);
  resumeContext = createResumeContext('anime', title, options);
  resumeContext.playUrl = url;
  resumeSeekTime = options.resumeTime || 0;
  lastResumeWrite = 0;
  try {
    const preference = preferredAudio();
    const params = new URLSearchParams({
      audio: preference === 'original' ? 'sub' : preference === 'auto' ? 'auto' : 'ita',
    });
    if (options.anilistId) {
      params.set('anilist_id', String(options.anilistId));
      params.set('title', title.split(' — ')[0]);
      params.set('episode', String(options.episode || 1));
    } else {
      params.set('url', url);
    }
    const data = await withLoading('Sto preparando la riproduzione...', () =>
      api(`/api/play/anime?${params}`, controller.signal));
    if (controller.signal.aborted || playbackController !== controller) return;
    const videoUrl = showSourceResult(
      data, data.source, data.referer || url,
      `${data.audio || 'Audio automatico'} · ${data.via || 'AnimeUnity'}`,
    );
    await prepareVideo(videoUrl, controller.signal);
    if (controller.signal.aborted || playbackController !== controller) return;
    openPlayer(title);
    startPreparedVideo();
  } catch (error) {
    if (error.name === 'AbortError' || controller.signal.aborted) return;
    if (playbackController === controller) {
      elements.player.classList.add('hidden');
      elements.player.classList.remove('is-preloading', 'is-resolving', 'is-ready', 'is-error');
      elements.video.pause();
      elements.video.removeAttribute('src');
      elements.video.load();
      document.body.classList.remove('player-open');
      $('detail-attribution').textContent = `Video non disponibile: ${error.message}`;
    }
  } finally {
    restoreButton();
    if (playbackController === controller) playbackController = null;
  }
}

async function openSerie(id, season, episode, title, options = {}) {
  persistResumeProgress(true);
  playbackController?.abort();
  nativeRecoveryAttempts = 0;
  hlsRecoveryAttempts = { network: 0, media: 0 };
  const controller = new AbortController();
  playbackController = controller;
  const restoreButton = setPlaybackButtonBusy(options.triggerButton);
  resumeContext = createResumeContext('serie', title, { ...options, season, episode });
  resumeContext.id = id;
  resumeContext.playUrl = '';
  resumeSeekTime = options.resumeTime || 0;
  lastResumeWrite = 0;
  try {
    const data = await withLoading('Sto preparando la riproduzione...', () =>
      api(`/api/play/serie?${new URLSearchParams({
        tid: id,
        title: title.split(' — ')[0],
        season: String(season),
        ep: String(episode),
        audio: preferredAudio(),
      })}`, controller.signal));
    if (controller.signal.aborted || playbackController !== controller) return;
    const variants = (data.variants || []).map(variant => `${variant.height}p`).join(', ');
    const videoUrl = showSourceResult(
      data, data.source, data.referer || '',
      `${data.via || 'StreamingCommunity'} · ${variants || 'qualità automatica'}`,
    );
    await prepareVideo(videoUrl, controller.signal);
    if (controller.signal.aborted || playbackController !== controller) return;
    openPlayer(title);
    startPreparedVideo();
  } catch (error) {
    if (error.name === 'AbortError' || controller.signal.aborted) return;
    if (playbackController === controller) {
      elements.player.classList.add('hidden');
      elements.player.classList.remove('is-preloading', 'is-resolving', 'is-ready', 'is-error');
      elements.video.pause();
      elements.video.removeAttribute('src');
      elements.video.load();
      document.body.classList.remove('player-open');
      $('detail-attribution').textContent = `Video non disponibile: ${error.message}`;
    }
  } finally {
    restoreButton();
    if (playbackController === controller) playbackController = null;
  }
}

function restoreHashRoute() {
  const hash = location.hash.replace(/^#\/?/, '');
  const [route, queryString = ''] = hash.split('?');
  if (!route) return false;
  const parts = route.split('/').map(decodeURIComponent);
  if (parts[0] === 'my-list') {
    renderFavorites();
    lastCatalogHash = '#/my-list';
    return true;
  }
  if (parts[0] === 'about') {
    showAbout({ syncRoute: false });
    return true;
  }
  if (parts[0] === 'catalog' && ['sc', 'au'].includes(parts[1])) {
    setSource(parts[1], { refresh: false });
    elements.query.value = '';
    loadHome({ syncRoute: false });
    return true;
  }
  if (parts[0] === 'search' && ['sc', 'au'].includes(parts[1])) {
    setSource(parts[1], { refresh: false });
    const params = new URLSearchParams(queryString);
    const query = params.get('q') || '';
    const kind = params.get('kind');
    setKind(['all', 'movie', 'tv'].includes(kind) ? kind : 'all');
    elements.query.value = query;
    lastCatalogHash = `#/search/${parts[1]}?${new URLSearchParams({ q: query, kind: elements.kind.value })}`;
    showSearchScreen();
    if (query.trim().length >= 2) runSearch(query, { syncRoute: false });
    else {
      elements.searchResults.replaceChildren();
      elements.searchResultsCount.textContent = '';
      elements.searchTitle.textContent = 'I risultati appariranno qui';
      showSearchStatus(query.trim()
        ? 'Inserisci almeno 2 caratteri per cercare.'
        : 'Inserisci un titolo o usa il tastierino per iniziare.');
    }
    return true;
  }
  if (parts[0] === 'anime' && parts[1]) {
    setSource('au', { refresh: false });
    openDetail('anime', parts[1], parts[2] || '');
    return true;
  }
  if (parts[0] === 'serie' && parts[1]) {
    setSource('sc', { refresh: false });
    openDetail('serie', parts[1], parts[2] || '');
    return true;
  }
  return false;
}

function handleSubmit(event) {
  event.preventDefault();
  clearTimeout(searchTimer);
  const query = elements.query.value.trim();
  if (query.length < 2) {
    showSearchScreen();
    showSearchStatus('Inserisci almeno 2 caratteri per cercare.');
    return;
  }
  closeSidebar();
  runSearch(query);
}

function wireControls() {
  const sidebar = $('app-sidebar');
  sidebar.addEventListener('pointerenter', () => document.body.classList.add('sidebar-hover'));
  sidebar.addEventListener('pointerleave', () => {
    if (!document.body.classList.contains('sidebar-open')) {
      document.body.classList.remove('sidebar-hover');
    }
  });
  sidebar.addEventListener('mouseleave', () => {
    if (!document.body.classList.contains('sidebar-open')) document.body.classList.remove('sidebar-hover');
  });
  sidebar.addEventListener('focusin', () => document.body.classList.add('sidebar-hover'));
  sidebar.addEventListener('focusout', () => {
    setTimeout(() => {
      if (!sidebar.contains(document.activeElement) && !document.body.classList.contains('sidebar-open')) {
        document.body.classList.remove('sidebar-hover');
      }
    }, 0);
  });
  $('nav-film-tv').addEventListener('click', () => {
    switchCatalog('sc');
  });
  $('nav-anime').addEventListener('click', () => {
    switchCatalog('au');
  });
  $('sidebar-toggle').addEventListener('click', () => {
    const expanded = document.body.classList.toggle('sidebar-open');
    document.body.classList.toggle('sidebar-hover', expanded);
    $('sidebar-toggle').setAttribute('aria-expanded', String(expanded));
  });
  document.addEventListener('pointerdown', event => {
    if (document.body.classList.contains('sidebar-open') && !sidebar.contains(event.target)) closeSidebar();
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape' && document.body.classList.contains('sidebar-open')) closeSidebar();
  });
  $('sidebar-home').addEventListener('click', () => {
    closeSidebar();
    document.querySelectorAll('.sidebar-icon').forEach(button => button.classList.remove('is-active'));
    $('sidebar-home').classList.add('is-active');
    showAbout();
  });
  document.querySelectorAll('[data-about-catalog]').forEach(button => {
    button.addEventListener('click', () => {
      closeSidebar();
      setSource(button.dataset.aboutCatalog);
    });
  });
  $('sidebar-search').addEventListener('click', () => {
    closeSidebar();
    showSearchScreen();
    lastCatalogHash = `#/search/${elements.source.value}`;
    history.replaceState(null, '', lastCatalogHash);
    if (elements.query.value.trim().length >= 2) runSearch(elements.query.value.trim());
    else showSearchStatus('Inserisci un titolo o usa il tastierino per iniziare.');
    elements.query.focus();
  });
  $('search-source-sc').addEventListener('click', () => switchSearchSource('sc'));
  $('search-source-au').addEventListener('click', () => switchSearchSource('au'));
  $('sidebar-favorites').addEventListener('click', () => {
    closeSidebar();
    lastCatalogHash = '#/my-list';
    history.replaceState(null, '', lastCatalogHash);
    renderFavorites();
  });
  $('sidebar-settings').addEventListener('click', openSettingsPage);
  $('reduce-motion').checked = localStorage.getItem(MOTION_STORAGE) === 'true';
  document.body.classList.toggle('reduce-motion', $('reduce-motion').checked);
  $('reduce-motion').addEventListener('change', event => {
    const enabled = event.target.checked;
    document.body.classList.toggle('reduce-motion', enabled);
    try {
      localStorage.setItem(MOTION_STORAGE, String(enabled));
    } catch (error) {
      console.error('Impossibile salvare la preferenza di movimento.', error);
      showStatus('Impossibile salvare questa preferenza nel browser.', true);
    }
  });
  const savedTheme = localStorage.getItem(THEME_STORAGE);
  applyTheme(savedTheme || 'red');
  document.querySelectorAll('[data-theme-option]').forEach(button => {
    button.addEventListener('click', () => {
      const theme = button.dataset.themeOption;
      applyTheme(theme);
      try {
        localStorage.setItem(THEME_STORAGE, theme);
      } catch (error) {
        console.error('Impossibile salvare il tema selezionato.', error);
        showStatus('Impossibile salvare il tema nel browser.', true);
      }
    });
  });
  const languageSelect = $('preferred-language');
  languageSelect.value = preferredAudio();
  enhanceSelect(languageSelect);
  languageSelect.addEventListener('change', event => {
    try {
      localStorage.setItem(AUDIO_STORAGE, event.target.value);
    } catch (error) {
      console.error('Impossibile salvare la preferenza audio.', error);
      showStatus('Impossibile salvare la preferenza audio nel browser.', true);
    }
  });
  elements.searchForm.addEventListener('submit', handleSubmit);
  elements.query.addEventListener('input', () => {
    clearTimeout(searchTimer);
    catalogRequest += 1;
    searchController?.abort();
    showSearchScreen();
    const query = elements.query.value.trim();
    if (query.length < 2) {
      elements.searchResults.replaceChildren();
      elements.searchResultsCount.textContent = '';
      elements.searchTitle.textContent = 'I risultati appariranno qui';
      lastCatalogHash = `#/search/${elements.source.value}${query ? `?${new URLSearchParams({ q: query, kind: elements.kind.value })}` : ''}`;
      history.replaceState(null, '', lastCatalogHash);
      showSearchStatus(query
        ? 'Inserisci almeno 2 caratteri per cercare.'
        : 'Inserisci un titolo o usa il tastierino per iniziare.');
      return;
    }
    searchTimer = setTimeout(() => runSearch(query), 450);
  });
  document.querySelectorAll('.search-keypad [data-key]').forEach(button => {
    button.addEventListener('click', () => {
      const { key } = button.dataset;
      const input = elements.query;
      const start = input.selectionStart ?? input.value.length;
      const end = input.selectionEnd ?? input.value.length;
      if (key === 'clear') {
        input.value = '';
      } else if (key === 'backspace') {
        const from = start === end && start > 0 ? start - 1 : start;
        input.value = input.value.slice(0, from) + input.value.slice(end);
        input.setSelectionRange(from, from);
      } else {
        const value = key === 'space' ? ' ' : key;
        input.value = input.value.slice(0, start) + value + input.value.slice(end);
        input.setSelectionRange(start + value.length, start + value.length);
      }
      input.dispatchEvent(new Event('input', { bubbles: true }));
      input.focus();
    });
  });
  elements.kindMenu.querySelectorAll('.custom-option').forEach(option => {
    option.addEventListener('click', () => {
      setKind(option.dataset.value);
      elements.kindSelect.classList.remove('is-open');
      elements.kindMenu.classList.add('hidden');
      elements.kindToggle.setAttribute('aria-expanded', 'false');
      if (elements.query.value.trim().length >= 2) runSearch(elements.query.value.trim());
      else showSearchStatus('Inserisci almeno 2 caratteri per cercare.');
    });
  });
  elements.kindToggle.addEventListener('click', () => {
    const isOpen = elements.kindToggle.getAttribute('aria-expanded') === 'true';
    elements.kindToggle.setAttribute('aria-expanded', String(!isOpen));
    elements.kindSelect.classList.toggle('is-open', !isOpen);
    elements.kindMenu.classList.toggle('hidden', isOpen);
  });
  document.addEventListener('click', event => {
    if (!elements.kindSelect.contains(event.target)) {
      elements.kindToggle.setAttribute('aria-expanded', 'false');
      elements.kindSelect.classList.remove('is-open');
      elements.kindMenu.classList.add('hidden');
    }
  });
  $('detail-back').addEventListener('click', () => {
    location.hash = lastCatalogHash;
    if (location.hash === lastCatalogHash) restoreHashRoute();
  });
  $('profile-settings').addEventListener('click', openSettingsPage);
  $('settings-back').addEventListener('click', closeSettingsPage);
  $('add-profile').addEventListener('click', () => openProfileForm());
  $('switch-profile').addEventListener('click', () => {
    elements.settingsScreen.classList.add('hidden');
    document.body.classList.remove('settings-open');
    elements.profileScreen.classList.remove('hidden');
    elements.page.classList.add('hidden');
    elements.profileControls.classList.add('hidden');
    renderProfilePicker();
  });
  $('cancel-profile-form').addEventListener('click', () => {
    if (settingsReturnToPicker) {
      elements.settingsScreen.classList.add('hidden');
      document.body.classList.remove('settings-open');
      elements.profileScreen.classList.remove('hidden');
      document.body.classList.add('profile-picker-open');
      settingsReturnToPicker = false;
      return;
    }
    openSettingsPage();
  });
  elements.profileForm.addEventListener('submit', saveProfile);
  $('profile-name').addEventListener('input', updateAvatarPreview);
  $('remove-avatar').addEventListener('click', () => {
    pendingAvatar = '';
    updateAvatarPreview();
    $('remove-avatar').disabled = true;
  });
  $('avatar-file').addEventListener('change', event => {
    const file = event.target.files?.[0];
    if (!file) return;
    if (!['image/png', 'image/jpeg', 'image/webp', 'image/gif'].includes(file.type)) {
      showProfileFormError('Scegli un’immagine PNG, JPEG, WebP o GIF.');
      event.target.value = '';
      return;
    }
    if (file.size > PROFILE_IMAGE_LIMIT) {
      showProfileFormError('L’immagine deve essere più piccola di 2 MB.');
      event.target.value = '';
      return;
    }
    const reader = new FileReader();
    reader.addEventListener('load', () => {
      pendingAvatar = String(reader.result || '');
      updateAvatarPreview();
      $('remove-avatar').disabled = !pendingAvatar;
      elements.profileError.classList.add('hidden');
    });
    reader.addEventListener('error', () => showProfileFormError('Impossibile leggere l’immagine selezionata.'));
    reader.readAsDataURL(file);
  });
  $('detail-watch').addEventListener('click', event => {
    if (!currentDetail) return;
    if (currentDetail.type === 'anime') {
      const episode = currentDetail.data.episodes?.[0];
      if (episode || isAnimeMovie(currentDetail.data)) openAnime('', episode
        ? episodePlaybackTitle(currentDetail.data.title, episode.label || 'Episodio 1')
        : currentDetail.data.title, {
        anilistId: currentDetail.data.anilist_id,
        episode: episode?.number || 1,
        triggerButton: event.currentTarget,
      });
      else {
        showStatus('Nessuna sorgente di riproduzione disponibile.', true);
      }
    } else openSerie(currentDetail.data.id, 1, 1, currentDetail.data.title, {
      triggerButton: event.currentTarget,
    });
  });
  elements.video.addEventListener('timeupdate', () => persistResumeProgress());
  elements.video.addEventListener('ended', () => {
    if (resumeContext) removeResumeItem(resumeContext);
    resumeContext = null;
  });
  elements.video.addEventListener('click', () => {
    if (elements.video.paused) {
      elements.video.play().catch(error => setPlayerError(`Impossibile avviare il video: ${error.message}`));
    } else {
      elements.video.pause();
    }
  });
  wirePlayerActions();
  document.addEventListener('fullscreenchange', () => {
    const active = document.fullscreenElement === elements.player;
    $('player-fullscreen').setAttribute('aria-label', active ? 'Esci dallo schermo intero' : 'Attiva schermo intero');
  });
  window.addEventListener('hashchange', () => {
    if (!restoreHashRoute()) {
      setSource(elements.source.value, { refresh: false });
      showCatalog();
      loadHome();
    }
  });
  document.addEventListener('keydown', event => {
    if (!elements.settingsScreen.classList.contains('hidden') && event.key === 'Escape') {
      event.preventDefault();
      closeSettingsPage();
      return;
    }
    const playerOpen = !elements.player.classList.contains('hidden');
    if (event.key === 'Escape' && playerOpen) {
      event.preventDefault();
      closePlayer();
      return;
    }
    if (!playerOpen || !(event.target instanceof HTMLElement)
      || /INPUT|SELECT|TEXTAREA/.test(event.target.tagName)
      || event.target.closest('button, a, details, label')) return;
    if (event.code === 'Space') {
      event.preventDefault();
      revealPlayerControls();
      elements.playerPlay.click();
    } else if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
      event.preventDefault();
      seekBy(event.key === 'ArrowLeft' ? -10 : 10);
    } else if (event.key.toLowerCase() === 'f') {
      revealPlayerControls();
      togglePlayerFullscreen();
    } else if (event.key.toLowerCase() === 'm') {
      elements.video.muted = !elements.video.muted;
    }
  });
  if (isTvDevice) {
    document.addEventListener('keydown', event => {
      if (!['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight'].includes(event.key)) return;
      if (!elements.player.classList.contains('hidden')) return;
      const focused = document.activeElement;
      if (focused instanceof HTMLElement
        && (/INPUT|SELECT|TEXTAREA/.test(focused.tagName) || focused.isContentEditable)) return;

      const candidates = [...document.querySelectorAll(
        'a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), select:not([disabled]), summary, [tabindex]:not([tabindex="-1"])',
      )].filter(element => {
        if (!(element instanceof HTMLElement) || element === focused || element.closest('[aria-hidden="true"]')) return false;
        if (element.closest('.hidden, [hidden]') || getComputedStyle(element).display === 'none'
          || getComputedStyle(element).visibility === 'hidden') return false;
        const rect = element.getBoundingClientRect();
        return rect.width > 0 && rect.height > 0;
      });

      const origin = focused instanceof HTMLElement ? focused.getBoundingClientRect() : null;
      const center = origin
        ? { x: origin.left + origin.width / 2, y: origin.top + origin.height / 2 }
        : { x: -1, y: -1 };
      const directions = {
        ArrowUp: { x: 0, y: -1 },
        ArrowDown: { x: 0, y: 1 },
        ArrowLeft: { x: -1, y: 0 },
        ArrowRight: { x: 1, y: 0 },
      };
      const direction = directions[event.key];
      const nearest = candidates.map(element => {
        const rect = element.getBoundingClientRect();
        const dx = rect.left + rect.width / 2 - center.x;
        const dy = rect.top + rect.height / 2 - center.y;
        const primary = dx * direction.x + dy * direction.y;
        const cross = Math.abs(dx * direction.y - dy * direction.x);
        return { element, score: primary > 0 ? primary + cross * 2.2 : Infinity };
      }).sort((left, right) => left.score - right.score)[0];

      if (nearest?.score < Infinity) {
        event.preventDefault();
        nearest.element.focus({ preventScroll: true });
        const verticalMove = event.key === 'ArrowUp' || event.key === 'ArrowDown';
        nearest.element.scrollIntoView({
          block: verticalMove ? 'center' : 'nearest',
          inline: 'nearest',
          behavior: document.body.classList.contains('reduce-motion') ? 'auto' : 'smooth',
        });
        return;
      }

      const rail = focused instanceof HTMLElement ? focused.closest('.catalog-rail') : null;
      if (rail && (event.key === 'ArrowLeft' || event.key === 'ArrowRight')) {
        const amount = Math.max(220, rail.clientWidth * 0.72);
        const direction = event.key === 'ArrowLeft' ? -1 : 1;
        if ((direction < 0 && rail.scrollLeft > 0)
          || (direction > 0 && rail.scrollLeft + rail.clientWidth < rail.scrollWidth - 2)) {
          event.preventDefault();
          rail.scrollBy({ left: amount * direction, behavior: 'smooth' });
        }
      } else if (event.key === 'ArrowUp' || event.key === 'ArrowDown') {
        event.preventDefault();
        window.scrollBy({
          top: event.key === 'ArrowUp' ? -window.innerHeight * 0.72 : window.innerHeight * 0.72,
          behavior: 'smooth',
        });
      }
    });
  }
}

wireControls();
setupPageScrollbar();
if (window.teenixWindow?.isElectron) {
  document.querySelectorAll('[data-window-action]').forEach(button => {
    button.addEventListener('click', () => {
      const action = button.dataset.windowAction;
      if (action === 'minimize') window.teenixWindow.minimize();
      else if (action === 'maximize') window.teenixWindow.toggleMaximize();
      else if (action === 'close') window.teenixWindow.close();
    });
  });
  window.teenixWindow.onMaximizeChange(maximized => {
    document.documentElement.classList.toggle('window-maximized', maximized);
    const button = document.querySelector('[data-window-action="maximize"]');
    button?.setAttribute('aria-label', maximized ? 'Ripristina finestra' : 'Ingrandisci');
  });
}
renderProfilePicker();
const savedSource = localStorage.getItem(SOURCE_STORAGE);
setSource(savedSource === 'au' ? 'au' : 'sc', { refresh: false, persist: false });
if (!profiles.some(profile => profile.id === activeProfileId)) activeProfileId = null;
elements.profileScreen.classList.remove('hidden');
document.body.classList.add('profile-picker-open');
