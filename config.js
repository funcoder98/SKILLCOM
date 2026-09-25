// Central place to point the frontend at your running FastAPI backend.
const API_BASE = "http://127.0.0.1:8000";

// Redirects to login.html if there's no auth token. Call at the top of any
// page that requires the user to be logged in.
function requireAuth() {
    if (!localStorage.getItem('authToken')) {
        window.location.href = "login.html";
        return null;
    }
    return localStorage.getItem('authToken');
}

function authHeaders() {
    return { "Authorization": `Bearer ${localStorage.getItem('authToken')}` };
}

function logout() {
    localStorage.removeItem('authToken');
    localStorage.removeItem('currentUser');
    window.location.href = "login.html";
}
