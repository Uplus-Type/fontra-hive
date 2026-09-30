// Fontra Hive account pages: sign in, accept an invitation (and sign up),
// forgot / reset password, sign out. They talk to hive-api (/api/…), which
// sets the HttpOnly cookies; nothing secret is kept in the page.
//
// Tokens from email links are in the URL fragment (#…): browsers never send
// it to a server, so it stays out of logs and Referer headers.
//
// Copyright (c) 2026 Jérémie Hornus / U+Type — GPLv3, see LICENSE.

export async function call(path, body, method = "POST") {
  const response = await fetch(path, {
    method,
    credentials: "same-origin",
    headers: body !== undefined ? { "Content-Type": "application/json" } : {},
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  let data = null;
  try {
    data = await response.json();
  } catch (error) {
    data = null;
  }
  if (!response.ok) {
    const error = new Error(messageOf(data) || `Error ${response.status}`);
    error.status = response.status;
    throw error;
  }
  return data;
}

function messageOf(data) {
  if (!data) return "";
  if (typeof data.detail === "string") return data.detail;
  if (Array.isArray(data.detail)) return "Please fill in every field.";
  return "";
}

// Where to go after signing in: a path on this site, never elsewhere.
export function safeRef(ref) {
  return typeof ref === "string" && ref.startsWith("/") && !ref.startsWith("//") ? ref : "/";
}

export function tokenFromFragment() {
  return decodeURIComponent(location.hash.replace(/^#/, ""));
}

const $ = (selector) => document.querySelector(selector);

function showError(form, error) {
  form.querySelector(".error").textContent = error ? error.message || String(error) : "";
}

async function busy(form, action) {
  const button = form.querySelector("button[type=submit]");
  button.disabled = true;
  showError(form, null);
  try {
    await action();
  } catch (error) {
    showError(form, error);
  } finally {
    button.disabled = false;
  }
}

function checkSamePasswords(form) {
  const [a, b] = form.querySelectorAll("input[type=password]");
  if (b && a.value !== b.value) throw new Error("The two passwords differ.");
  return a.value;
}

// --- pages -------------------------------------------------------------------------

async function loginPage() {
  const ref = safeRef(new URLSearchParams(location.search).get("ref"));
  // Still signed in for 30 days: a new short token is enough.
  try {
    await call("/api/auth/refresh");
    location.replace(ref);
    return;
  } catch (error) {
    // not signed in: show the form
  }
  const form = $("#login");
  form.classList.remove("hidden");
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    busy(form, async () => {
      await call("/api/auth/login", { login: form.login.value.trim(), password: form.password.value });
      location.replace(ref);
    });
  });
  form.login.focus();
  waitlist();
}

// "Request an invitation" under the sign-in form (sign-up is by invitation
// only). Opened directly by /#request, e.g. from a landing page.
function waitlist() {
  const open = $("#request-open");
  const form = $("#request");
  const sent = $("#request-sent");
  if (!open || !form) return;
  const show = () => {
    open.classList.add("hidden");
    form.classList.remove("hidden");
    form.email.focus({ preventScroll: true });
    form.scrollIntoView({ behavior: "smooth", block: "center" });
  };
  open.addEventListener("click", show);
  if (location.hash === "#request") show();
  // Links to #request further down the page open it too.
  window.addEventListener("hashchange", () => location.hash === "#request" && show());
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    busy(form, async () => {
      const email = form.email.value.trim();
      await call("/api/waitlist", {
        email,
        name: form.fullname.value.trim(),
        organization: form.organization.value.trim(),
        message: form.message.value.trim(),
        website: form.website.value,
      });
      form.classList.add("hidden");
      sent.textContent = `Thank you. We will write to ${email} with an invitation when the beta opens.`;
      sent.classList.remove("hidden");
    });
  });
}

async function invitationPage() {
  const token = tokenFromFragment();
  const card = $("#invitation");
  let info;
  try {
    info = await call("/api/invitations/lookup", { token });
  } catch (error) {
    $(".lead").textContent = "This invitation link is not valid any more. Ask for a new one.";
    return;
  }
  const inv = info.invitation;
  const inviter = inv.invitedBy ? inv.invitedBy.name : "Hive";
  $(".lead").textContent =
    `${inviter} invited ${inv.email} to ` +
    (inv.project ? `work on ${inv.project} as ${inv.role}` : inv.organization ? `join ${inv.target.replace(/^organization /, "")} as ${inv.role}` : "join Hive") +
    ".";
  card.classList.remove("hidden");

  let signedIn = null;
  try {
    signedIn = (await call("/api/me", undefined, "GET")).user;
  } catch (error) {
    try {
      signedIn = (await call("/api/auth/refresh")).user;
    } catch (error2) {
      signedIn = null;
    }
  }
  const accept = async () => {
    await call("/api/invitations/accept", { token });
    location.replace("/");
  };

  if (signedIn && signedIn.email.toLowerCase() === inv.email.toLowerCase()) {
    const form = $("#accept");
    form.classList.remove("hidden");
    form.querySelector(".as").textContent = `Signed in as ${signedIn.name} (${signedIn.username}).`;
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      busy(form, accept);
    });
  } else if (info.hasAccount) {
    const form = $("#signin");
    form.classList.remove("hidden");
    if (signedIn) {
      form.querySelector(".note").textContent =
        `You are signed in as ${signedIn.username}, but this invitation is for ${inv.email}: sign in with that account.`;
    }
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      busy(form, async () => {
        await call("/api/auth/login", { login: form.login.value.trim(), password: form.password.value });
        await accept();
      });
    });
  } else {
    const form = $("#signup");
    form.classList.remove("hidden");
    form.querySelector(".email").textContent = inv.email;
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      busy(form, async () => {
        const password = checkSamePasswords(form);
        await call("/api/invitations/signup", {
          token,
          username: form.username.value.trim(),
          name: form.elements.fullname.value.trim(), // not form.name: that is the form's own name
          password,
        });
        location.replace("/");
      });
    });
    form.username.focus();
  }
}

function forgotPage() {
  const form = $("#forgot");
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    busy(form, async () => {
      await call("/api/auth/password/forgot", { email: form.email.value.trim() });
      form.classList.add("hidden");
      $(".lead").textContent =
        "If an account uses this address, we sent it a link to choose a new password. It is valid for one hour.";
    });
  });
  form.email.focus();
}

function resetPage() {
  const token = tokenFromFragment();
  const form = $("#reset");
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    busy(form, async () => {
      const password = checkSamePasswords(form);
      await call("/api/auth/password/reset", { token, password });
      location.replace("/");
    });
  });
}

async function logoutPage() {
  try {
    await call("/api/auth/logout");
  } finally {
    location.replace("/");
  }
}

const PAGES = {
  login: loginPage,
  invitation: invitationPage,
  forgot: forgotPage,
  reset: resetPage,
  logout: logoutPage,
};

if (!window.__hiveAccountNoAutoStart) PAGES[document.body.dataset.page]?.();
