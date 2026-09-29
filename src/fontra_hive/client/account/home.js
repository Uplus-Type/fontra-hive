// Hive's home page: projects (list, create, import a font, settings, trash),
// organizations (members, invitations, base role) and the profile (name,
// photo, password, sessions). Everything goes to hive-api (/api/…), except
// importing a font, which the Fontra server does (/api/hive/…/import).
//
// One page, sections chosen by the URL fragment:
//   #                        projects          #new             new project
//   #project/<owner>/<name>  project settings  #orgs            organizations
//   #org/<login>             one organization  #profile         profile
//
// Copyright (c) 2026 Jérémie Hornus / U+Type — GPLv3, see LICENSE.

import { call } from "./account.js";
import { avatar, hiveApiProjectPath, openShareDialog } from "../views/hive-views.js";

const ROLES = ["observer", "reviewer", "designer", "manager", "admin"];
const main = () => document.getElementById("main");
let me = null;

// --- helpers -------------------------------------------------------------------------

export function el(tag, attrs = {}, children = []) {
  const element = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (key === "class") element.className = value;
    else if (key.startsWith("on")) element.addEventListener(key.slice(2), value);
    else if (key === "value") element.value = value;
    else if (value !== undefined && value !== null && value !== false) element.setAttribute(key, value === true ? "" : value);
  }
  for (const child of [].concat(children)) {
    if (child !== null && child !== undefined && child !== false) element.append(child);
  }
  return element;
}

const get = (path) => call(path, undefined, "GET");
const openURL = (projectId) => `/fontoverview.html?project=${encodeURIComponent(projectId)}`;
const projectHash = (projectId) => "#project/" + projectId.split("/").map(encodeURIComponent).join("/");

function form(fields, submitLabel, onSubmit) {
  const error = el("div", { class: "error" });
  const ok = el("div", { class: "ok" });
  const button = el("button", { type: "submit" }, [submitLabel]);
  const element = el("form", {}, [...fields, error, ok, el("div", { class: "row" }, [button])]);
  element.addEventListener("submit", async (event) => {
    event.preventDefault();
    button.disabled = true;
    error.textContent = ok.textContent = "";
    try {
      const message = await onSubmit(element);
      if (typeof message === "string") ok.textContent = message;
    } catch (e) {
      error.textContent = e.message;
    } finally {
      button.disabled = false;
    }
  });
  return element;
}

function field(label, input, hint) {
  return el("label", {}, [label, hint ? el("small", {}, [hint]) : null, input]);
}

function roleSelect(roles, selected, attrs = {}) {
  return el("select", attrs, roles.map((r) => el("option", { value: r, selected: r === selected }, [r])));
}

// Upload a font to a project, with progress (fetch cannot report it).
export function uploadFont(projectId, file, onProgress) {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open("POST", `/api/hive/projects/${encodeURIComponent(projectId)}/import`);
    request.upload.addEventListener("progress", (e) => e.lengthComputable && onProgress?.(e.loaded / e.total));
    request.addEventListener("load", () => {
      if (request.status >= 200 && request.status < 300) resolve(JSON.parse(request.responseText));
      else reject(new Error(request.responseText || `Error ${request.status}`));
    });
    request.addEventListener("error", () => reject(new Error("The upload failed.")));
    const body = new FormData();
    body.append("file", file);
    request.send(body);
  });
}

const FONT_ACCEPT = ".zip,.ttf,.otf,.woff,.woff2,.ttx,.glyphs,.designspace";
const FONT_HINT =
  "A font that is a folder (.designspace with its UFOs, .ufo, .fontra, .glyphspackage) as a .zip; " +
  "or a single file: .ttf, .otf, .woff2, .glyphs…";

function importFields() {
  const input = el("input", { type: "file", name: "font", accept: FONT_ACCEPT });
  const bar = el("div", { class: "progress hidden" }, [el("div")]);
  return { input, bar, elements: [field("Font", input, FONT_HINT), bar] };
}

async function importWithProgress(projectId, file, bar, status) {
  bar.classList.remove("hidden");
  const fill = bar.firstChild;
  status.textContent = `Uploading ${file.name}…`;
  await uploadFont(projectId, file, (fraction) => {
    fill.style.width = `${Math.round(fraction * 100)}%`;
    if (fraction >= 1) status.textContent = `Converting ${file.name}…`;
  });
  fill.style.width = "100%";
}

// --- invitations waiting for me ------------------------------------------------------

async function showInvitations() {
  const box = document.getElementById("invitations");
  box.replaceChildren();
  let pending = [];
  try {
    pending = (await get("/api/invitations/mine")).invitations;
  } catch (error) {
    return;
  }
  for (const inv of pending) {
    const who = inv.invitedBy ? inv.invitedBy.name : "Hive";
    const what = inv.project ? `${inv.project} as ${inv.role}` : inv.organization ? `the organization ${inv.organization} as ${inv.role}` : "Hive";
    box.append(
      el("div", { class: "invitation" }, [
        el("span", {}, [`${who} invited you to ${what}.`]),
        el("button", {
          onclick: async () => {
            await call(`/api/invitations/${inv.id}/accept`);
            await showInvitations();
            route();
          },
        }, ["Accept"]),
      ])
    );
  }
}

// --- projects ---------------------------------------------------------------------------

function projectCard(p) {
  return el("a", { class: "project-card", href: openURL(p.id), "data-project": p.id }, [
    el("div", { class: "owner" }, [p.owner]),
    el("div", { class: "name" }, [p.name]),
    el("div", { class: "description" }, [p.description || ""]),
    el("div", { class: "foot" }, [
      el("span", { class: `badge ${p.role}` }, [p.role]),
      el("button", {
        class: "link-button",
        type: "button",
        onclick: (event) => {
          event.preventDefault();
          location.hash = projectHash(p.id);
        },
      }, ["Settings"]),
    ]),
  ]);
}

async function projectsSection() {
  const [{ projects }, { projects: trashed }] = await Promise.all([
    get("/api/me/projects"),
    get("/api/me/projects?trashed=true"),
  ]);
  const content = [
    el("h2", {}, [el("span", { class: "grow" }, ["Projects"]), el("a", { href: "#new" }, [el("button", {}, ["New project"])])]),
  ];
  if (!projects.length) {
    content.push(
      el("p", { class: "empty" }, [
        "No project yet. Create one, empty or from a font you already have,",
        el("br"),
        "or ask someone to invite you to theirs.",
      ])
    );
  } else {
    const byOwner = new Map();
    for (const p of projects) {
      if (!byOwner.has(p.owner)) byOwner.set(p.owner, []);
      byOwner.get(p.owner).push(p);
    }
    for (const [owner, list] of byOwner) {
      content.push(el("h3", {}, [owner === me.username ? "Yours" : owner]), el("div", { class: "grid" }, list.map(projectCard)));
    }
  }
  if (trashed.length) {
    const details = el("details", {}, [el("summary", {}, [`Trash (${trashed.length})`])]);
    for (const p of trashed) {
      details.append(
        el("div", { class: "row", style: "margin:0.5em 0" }, [
          el("span", { class: "grow" }, [p.id]),
          el("button", {
            class: "secondary",
            onclick: async () => {
              await call(`${hiveApiProjectPath(p.id)}/restore`);
              route();
            },
          }, ["Restore"]),
        ])
      );
    }
    content.push(el("h3", {}, [""]), details);
  }
  main().replaceChildren(...content);
}

async function newProjectSection() {
  const { organizations } = await get("/api/me/organizations");
  const owners = [me.username, ...organizations.filter((o) => o.role === "owner").map((o) => o.login)];
  const owner = el("select", { name: "owner" }, owners.map((o) => el("option", { value: o }, [o === me.username ? `${o} (you)` : o])));
  const name = el("input", { name: "name", required: true, maxlength: 100, pattern: "[A-Za-z0-9._\\-]+" });
  const description = el("input", { name: "description", maxlength: 300 });
  const empty = el("input", { type: "radio", name: "start", value: "empty", checked: true });
  const fromFont = el("input", { type: "radio", name: "start", value: "font" });
  const { input: file, bar, elements: fontFields } = importFields();
  const fontBox = el("div", { class: "hidden" }, fontFields);
  const toggle = () => fontBox.classList.toggle("hidden", !fromFont.checked);
  empty.addEventListener("change", toggle);
  fromFont.addEventListener("change", toggle);
  const status = el("p", { class: "note" });
  const f = form(
    [
      field("Owner", owner, organizations.some((o) => o.role === "owner") ? "you, or an organization you own" : null),
      field("Name", name, "letters, digits, '-', '_' and '.'"),
      field("Description", description, "optional"),
      el("div", { class: "choices" }, [
        el("label", {}, [empty, "Start with an empty font"]),
        el("label", {}, [fromFont, "Import a font"]),
      ]),
      fontBox,
      status,
    ],
    "Create the project",
    async () => {
      if (fromFont.checked && !file.files[0]) throw new Error("Choose a font file, or start with an empty font.");
      const { project } = await call("/api/projects", {
        name: name.value.trim(),
        owner: owner.value,
        description: description.value.trim(),
      });
      if (fromFont.checked) {
        try {
          await importWithProgress(project.id, file.files[0], bar, status);
        } catch (error) {
          location.hash = projectHash(project.id);
          throw new Error(`The project was created, but the font could not be imported: ${error.message}`);
        }
      }
      location.href = openURL(project.id);
    }
  );
  main().replaceChildren(el("h2", {}, ["New project"]), el("div", { class: "panel" }, [f]));
  name.focus();
}

async function projectSection(projectId) {
  const base = hiveApiProjectPath(projectId);
  const { project } = await get(base);
  const admin = project.capabilities.includes("administer");
  const content = [
    el("h2", {}, [
      el("span", { class: "grow" }, [project.id, project.trashed ? " (in the trash)" : ""]),
      project.trashed ? null : el("a", { href: openURL(project.id) }, [el("button", {}, ["Open"])]),
    ]),
    el("p", { class: "note" }, [`Your role: ${project.role}.`]),
  ];

  // People
  content.push(
    el("div", { class: "panel" }, [
      el("h3", { style: "margin-top:0" }, ["People"]),
      el("p", { class: "note" }, ["Who has access, with which role; invite people by username or email."]),
      el("button", { onclick: () => openShareDialog(project.id) }, ["Manage people…"]),
    ])
  );

  if (admin) {
    const name = el("input", { value: project.name, required: true, maxlength: 100 });
    const description = el("input", { value: project.description, maxlength: 300 });
    content.push(
      el("div", { class: "panel" }, [
        el("h3", { style: "margin-top:0" }, ["General"]),
        form([field("Name", name), field("Description", description)], "Save", async () => {
          const { project: saved } = await call(base, { name: name.value.trim(), description: description.value.trim() }, "PATCH");
          if (saved.id !== project.id) location.hash = projectHash(saved.id);
          return "Saved.";
        }),
      ])
    );
    const { input: file, bar, elements } = importFields();
    const status = el("p", { class: "note" });
    content.push(
      el("div", { class: "panel" }, [
        el("h3", { style: "margin-top:0" }, ["Import a font"]),
        el("p", { class: "note" }, [
          "Replaces the project's font with the one you upload, as a new version: the history before it stays, and you can go back to it.",
        ]),
        form([...elements, status], "Import", async () => {
          if (!file.files[0]) throw new Error("Choose a font file.");
          await importWithProgress(project.id, file.files[0], bar, status);
          status.textContent = "";
          return `Imported ${file.files[0].name}.`;
        }),
      ])
    );
    content.push(
      el("div", { class: "panel" }, [
        el("h3", { style: "margin-top:0" }, [project.trashed ? "In the trash" : "Trash"]),
        project.trashed
          ? el("button", { onclick: async () => { await call(`${base}/restore`); route(); } }, ["Restore the project"])
          : el("button", {
              class: "danger",
              onclick: async () => {
                if (!confirm(`Move ${project.id} to the trash? Nobody will see it any more; you can restore it from your projects.`)) return;
                await call(base, undefined, "DELETE");
                location.hash = "";
              },
            }, ["Move to the trash"]),
      ])
    );
  }
  main().replaceChildren(...content);
}

// --- organizations ---------------------------------------------------------------------

async function orgsSection() {
  const { organizations } = await get("/api/me/organizations");
  const login = el("input", { required: true, maxlength: 39, pattern: "[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}" });
  const name = el("input", { maxlength: 100 });
  main().replaceChildren(
    el("h2", {}, ["Organizations"]),
    organizations.length
      ? el("div", { class: "grid" }, organizations.map((o) =>
          el("a", { class: "project-card", href: `#org/${encodeURIComponent(o.login)}` }, [
            el("div", { class: "name" }, [o.name]),
            el("div", { class: "owner" }, [o.login]),
            el("div", { class: "foot" }, [el("span", { class: "badge" }, [o.role])]),
          ])))
      : el("p", { class: "empty" }, ["You are not in any organization."]),
    el("div", { class: "panel", style: "margin-top:1.5em" }, [
      el("h3", { style: "margin-top:0" }, ["New organization"]),
      el("p", { class: "note" }, ["A studio or a team: its projects are shared by its members. You will be its owner."]),
      form([field("Short name", login, "in addresses: letters, digits, hyphens"), field("Full name", name)], "Create", async () => {
        const { organization } = await call("/api/orgs", { login: login.value.trim(), name: name.value.trim() });
        location.hash = `#org/${encodeURIComponent(organization.login)}`;
      }),
    ])
  );
}

async function orgSection(login) {
  const base = `/api/orgs/${encodeURIComponent(login)}`;
  const [{ organization: org }, members] = await Promise.all([get(base), get(`${base}/members`)]);
  const owner = members.canManage;
  const content = [el("h2", {}, [org.name, el("span", { class: "badge" }, [org.login])])];

  const rows = members.members.map((m) => {
    const isYou = m.username === me.username;
    return el("tr", { "data-username": m.username }, [
      el("td", { class: "who" }, [avatar(m, 28), el("div", {}, [`${m.name}${isYou ? " (you)" : ""}`, el("small", {}, [[m.username, m.email].filter(Boolean).join(" · ")])])]),
      el("td", {}, [
        owner
          ? roleSelect(["owner", "member"], m.role, {
              "aria-label": `Role of ${m.name}`,
              onchange: async (e) => {
                try {
                  await call(`${base}/members/${encodeURIComponent(m.username)}`, { role: e.target.value }, "PATCH");
                } catch (error) {
                  alertInPage(error.message);
                }
                route();
              },
            })
          : m.role,
      ]),
      el("td", {}, [
        owner || isYou
          ? el("button", {
              class: "secondary",
              onclick: async () => {
                try {
                  await call(`${base}/members/${encodeURIComponent(m.username)}`, undefined, "DELETE");
                } catch (error) {
                  alertInPage(error.message);
                  return;
                }
                if (isYou) location.hash = "#orgs";
                else route();
              },
            }, [isYou ? "Leave" : "Remove"])
          : null,
      ]),
    ]);
  });
  content.push(el("div", { class: "panel" }, [el("h3", { style: "margin-top:0" }, ["Members"]), el("table", { class: "people" }, rows)]));

  if (owner) {
    const { invitations } = await get(`${base}/invitations`);
    const email = el("input", { type: "email", required: true });
    const role = roleSelect(["member", "owner"], "member");
    content.push(
      el("div", { class: "panel" }, [
        el("h3", { style: "margin-top:0" }, ["Invite"]),
        invitations.length
          ? el("table", { class: "people" }, invitations.map((inv) =>
              el("tr", {}, [
                el("td", {}, [inv.email, el("small", {}, [`invited as ${inv.role}`])]),
                el("td", {}, [el("button", { class: "secondary", onclick: async () => { await call(`${base}/invitations/${inv.id}`, undefined, "DELETE"); route(); } }, ["Cancel"])]),
              ])))
          : null,
        form([el("div", { class: "row" }, [field("Email", email), field("As", role)])], "Send the invitation", async () => {
          await call(`${base}/invitations`, { email: email.value.trim(), role: role.value });
          const sent = email.value.trim();
          route();
          return `Invitation sent to ${sent}.`;
        }),
      ])
    );
    const name = el("input", { value: org.name, maxlength: 100 });
    const baseRole = el("select", {}, [
      el("option", { value: "" }, ["none: only the projects they are invited to"]),
      ...ROLES.filter((r) => r !== "admin").map((r) => el("option", { value: r, selected: r === org.baseRole }, [r])),
    ]);
    content.push(
      el("div", { class: "panel" }, [
        el("h3", { style: "margin-top:0" }, ["Settings"]),
        form([field("Full name", name), field("Members' role on every project", baseRole, "owners are admins of every project")], "Save", async () => {
          await call(base, { name: name.value.trim(), baseRole: baseRole.value }, "PATCH");
          return "Saved.";
        }),
      ])
    );
  }
  main().replaceChildren(...content);
}

function alertInPage(message) {
  const box = el("div", { class: "error", style: "margin:1em 0" }, [message]);
  main().prepend(box);
  setTimeout(() => box.remove(), 6000);
}

// --- profile ------------------------------------------------------------------------

async function profileSection() {
  const { user } = await get("/api/me");
  me = { ...me, ...user };
  const name = el("input", { value: user.name, maxlength: 100 });
  const photo = el("input", { type: "file", accept: "image/png,image/jpeg,image/gif,image/webp" });
  const current = el("input", { type: "password", autocomplete: "current-password", required: true });
  const next = el("input", { type: "password", autocomplete: "new-password", required: true, minlength: 10 });
  const { sessions } = await call("/api/auth/sessions", undefined, "GET");

  const photoError = el("div", { class: "error" });
  photo.addEventListener("change", async () => {
    if (!photo.files[0]) return;
    const body = new FormData();
    body.append("file", photo.files[0]);
    const response = await fetch("/api/me/avatar", { method: "POST", body, credentials: "same-origin" });
    if (!response.ok) {
      photoError.textContent = (await response.json().catch(() => ({}))).detail || "Could not use this image.";
      return;
    }
    route();
  });

  main().replaceChildren(
    el("h2", {}, ["Profile"]),
    el("div", { class: "panel" }, [
      el("div", { class: "photo" }, [
        avatar(user, 64),
        el("div", {}, [
          el("b", {}, [user.name]),
          el("div", { class: "note" }, [`${user.username} · ${user.email}`]),
          el("div", { class: "row", style: "margin-top:0.6em" }, [
            field("", photo),
            user.avatar ? el("button", { class: "secondary", onclick: async () => { await call("/api/me/avatar", undefined, "DELETE"); route(); } }, ["Remove the photo"]) : null,
          ]),
          photoError,
        ]),
      ]),
      form([field("Your name", name, "as shown to your team")], "Save", async () => {
        await call("/api/me", { name: name.value.trim() }, "PATCH");
        return "Saved.";
      }),
    ]),
    el("div", { class: "panel" }, [
      el("h3", { style: "margin-top:0" }, ["Password"]),
      form([field("Current password", current), field("New password", next, "at least 10 characters")], "Change the password", async () => {
        await call("/api/auth/password/change", { current: current.value, password: next.value });
        current.value = next.value = "";
        return "Changed. You were signed out everywhere else.";
      }),
    ]),
    el("div", { class: "panel" }, [
      el("h3", { style: "margin-top:0" }, ["Where you are signed in"]),
      el("table", { class: "people" }, sessions.map((s) =>
        el("tr", {}, [
          el("td", {}, [s.userAgent || "Unknown browser", el("small", {}, [`last used ${new Date(s.lastUsedAt).toLocaleString()}${s.current ? " · this browser" : ""}`])]),
          el("td", {}, [s.current ? null : el("button", { class: "secondary", onclick: async () => { await call(`/api/auth/sessions/${s.id}`, undefined, "DELETE"); route(); } }, ["Sign out"])]),
        ]))),
      el("div", { class: "row", style: "margin-top:1em" }, [
        el("button", { class: "secondary", onclick: async () => { await call("/api/auth/logout-all"); location.replace("/"); } }, ["Sign out everywhere"]),
      ]),
    ])
  );
}

// --- routing ----------------------------------------------------------------------------

export function parseHash(hash) {
  const parts = hash.replace(/^#/, "").split("/").map(decodeURIComponent);
  switch (parts[0]) {
    case "":
      return { section: "projects" };
    case "new":
      return { section: "projects", view: "new" };
    case "project":
      return parts.length === 3 ? { section: "projects", view: "project", project: `${parts[1]}/${parts[2]}` } : { section: "projects" };
    case "orgs":
      return { section: "orgs" };
    case "org":
      return { section: "orgs", view: "org", login: parts[1] };
    case "profile":
      return { section: "profile" };
    default:
      return { section: "projects" };
  }
}

async function route() {
  const where = parseHash(location.hash);
  for (const link of document.querySelectorAll("nav a")) {
    link.classList.toggle("current", link.dataset.section === where.section);
  }
  try {
    if (where.view === "new") await newProjectSection();
    else if (where.view === "project") await projectSection(where.project);
    else if (where.view === "org") await orgSection(where.login);
    else if (where.section === "orgs") await orgsSection();
    else if (where.section === "profile") await profileSection();
    else await projectsSection();
  } catch (error) {
    if (error.status === 401) {
      location.replace("/");
      return;
    }
    main().replaceChildren(el("p", { class: "error" }, [error.status === 404 ? "Not found." : error.message]));
  }
}

export async function start() {
  try {
    me = (await get("/api/me")).user;
  } catch (error) {
    location.replace("/"); // not signed in any more
    return;
  }
  window.addEventListener("hashchange", route);
  await Promise.all([showInvitations(), route()]);
}

if (!window.__hiveHomeNoAutoStart) start();
