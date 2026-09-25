// app-ui.js — shared across every Skillcom page.
// Handles: dark/light theme persistence, the animated top loading bar,
// and the pull-out drawer menu (hamburger -> Account / Meetings / Settings).

// ---------------------------------------------------------------------------
// Theme (dark / light)
// ---------------------------------------------------------------------------

const THEME_KEY = "skillcom-theme";

function getStoredTheme() {
    return localStorage.getItem(THEME_KEY) || "dark";
}

function applyTheme(theme) {
    document.documentElement.setAttribute("data-theme", theme);
    localStorage.setItem(THEME_KEY, theme);
    document.querySelectorAll(".theme-switch input").forEach(el => {
        el.checked = theme === "light";
    });
}

function toggleTheme() {
    applyTheme(getStoredTheme() === "dark" ? "light" : "dark");
}

// Apply immediately (before the rest of the DOM builds) so there's no flash
// of the wrong theme.
applyTheme(getStoredTheme());

// ---------------------------------------------------------------------------
// Top loading bar with an alternating skill icon (coding / building / video)
// ---------------------------------------------------------------------------

const LOADER_ICONS = ["💻", "🏗️", "📼"];
let _loaderIconTimer = null;
let _loaderIconIndex = 0;
let _activeRequests = 0;

function _ensureLoaderEl() {
    let el = document.getElementById("skillcomLoader");
    if (!el) {
        el = document.createElement("div");
        el.id = "skillcomLoader";
        el.innerHTML = `<div class="loader-fill"></div><div class="loader-icon">💻</div>`;
        document.body.prepend(el);
    }
    return el;
}

function showLoader() {
    _activeRequests++;
    const el = _ensureLoaderEl();
    el.classList.add("active");
    if (!_loaderIconTimer) {
        const iconEl = el.querySelector(".loader-icon");
        _loaderIconTimer = setInterval(() => {
            _loaderIconIndex = (_loaderIconIndex + 1) % LOADER_ICONS.length;
            iconEl.textContent = LOADER_ICONS[_loaderIconIndex];
        }, 500);
    }
}

function hideLoader() {
    _activeRequests = Math.max(0, _activeRequests - 1);
    if (_activeRequests > 0) return;
    const el = document.getElementById("skillcomLoader");
    if (el) el.classList.remove("active");
    if (_loaderIconTimer) {
        clearInterval(_loaderIconTimer);
        _loaderIconTimer = null;
    }
}

// Show the bar for the initial page load...
showLoader();
window.addEventListener("load", () => setTimeout(hideLoader, 150));

// ...and automatically for every fetch() call, so any page-to-page or
// in-page request shows the same animated bar without every page having to
// call showLoader()/hideLoader() by hand.
const _nativeFetch = window.fetch;
window.fetch = function (...args) {
    showLoader();
    return _nativeFetch.apply(this, args).finally(hideLoader);
};

// ---------------------------------------------------------------------------
// Pull-out drawer menu
// ---------------------------------------------------------------------------

function _drawerAvatarHtml() {
    const user = JSON.parse(localStorage.getItem("currentUser") || "{}");
    if (user.photo_url) {
        const base = typeof API_BASE !== "undefined" ? API_BASE : "";
        return `<img class="drawer-avatar" src="${base}${user.photo_url}" alt="${user.name || "Profile"}">`;
    }
    return `<div class="drawer-avatar-placeholder">🎓</div>`;
}

function _buildDrawer() {
    const user = JSON.parse(localStorage.getItem("currentUser") || "{}");
    const theme = getStoredTheme();

    const overlay = document.createElement("div");
    overlay.className = "drawer-overlay";
    overlay.id = "drawerOverlay";

    const drawer = document.createElement("div");
    drawer.className = "app-drawer";
    drawer.id = "appDrawer";

    drawer.innerHTML = `
        <div class="drawer-profile">
            ${_drawerAvatarHtml()}
            <div>
                <div style="font-weight:600;">${user.name || "My Account"}</div>
                <div style="color:var(--text-muted); font-size:0.8rem;">@${user.username || ""}</div>
            </div>
        </div>
        <nav class="drawer-nav">
            <a class="drawer-link" href="profile.html"><span class="drawer-icon">👤</span> Account</a>
            <a class="drawer-link" href="meetings.html"><span class="drawer-icon">🤝</span> Meetings</a>
            ${user.role === "admin" ? '<a class="drawer-link" href="admin-dashboard.html"><span class="drawer-icon">🛡️</span> Admin Panel</a>' : ''}
            <button type="button" class="drawer-link" id="drawerSettingsToggle"><span class="drawer-icon">⚙️</span> Settings</button>
            <div class="drawer-settings-panel" id="drawerSettingsPanel">
                <div class="theme-toggle-row">
                    <span>Dark mode</span>
                    <label class="theme-switch">
                        <input type="checkbox" id="themeSwitchInput" ${theme === "light" ? "checked" : ""}>
                        <span class="slider"></span>
                    </label>
                    <span>Light mode</span>
                </div>
            </div>
        </nav>
        <div class="drawer-footer">
            <button type="button" class="drawer-link" id="drawerLogoutBtn"><span class="drawer-icon">🚪</span> Log out</button>
        </div>
    `;

    document.body.appendChild(overlay);
    document.body.appendChild(drawer);

    overlay.addEventListener("click", closeDrawer);
    drawer.querySelector("#drawerSettingsToggle").addEventListener("click", () => {
        drawer.querySelector("#drawerSettingsPanel").classList.toggle("open");
    });
    drawer.querySelector("#themeSwitchInput").addEventListener("change", toggleTheme);
    drawer.querySelector("#drawerLogoutBtn").addEventListener("click", () => {
        if (typeof logout === "function") logout();
    });
}

function openDrawer() {
    if (!document.getElementById("appDrawer")) _buildDrawer();
    document.getElementById("drawerOverlay").classList.add("open");
    document.getElementById("appDrawer").classList.add("open");
}

function closeDrawer() {
    const overlay = document.getElementById("drawerOverlay");
    const drawer = document.getElementById("appDrawer");
    if (overlay) overlay.classList.remove("open");
    if (drawer) drawer.classList.remove("open");
}

function toggleDrawer() {
    const drawer = document.getElementById("appDrawer");
    if (drawer && drawer.classList.contains("open")) {
        closeDrawer();
    } else {
        openDrawer();
    }
}

// Wire up any hamburger button already in the page markup.
document.addEventListener("DOMContentLoaded", () => {
    const btn = document.getElementById("hamburgerBtn");
    if (btn) btn.addEventListener("click", toggleDrawer);
});
