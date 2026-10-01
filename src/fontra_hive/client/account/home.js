// Hive's home page: projects (list, create, import a font, settings, trash),
// organizations (members, invitations, base role) and the profile (name,
// photo, password, sessions). Everything goes to hive-api (/api/…), except
// importing a font, which the Fontra server does (/api/hive/…/import).
//
// One page, sections chosen by the URL fragment:
//   #                        projects          #new             new project
//   #project/<owner>/<name>  project settings  #orgs            organizations
//   #project/<owner>/<name>/comments           the project's comments
//   #org/<login>             one organization  #profile         profile
//
// Copyright (c) 2026 Jérémie Hornus / U+Type — GPLv3, see LICENSE.

import { call } from "./account.js";
import {
  avatar,
  branchNameProblem,
  compareWithDefault,
  exportDownload,
  hiveApiProjectPath,
  mayRestore,
  openMergeDialog,
  openRestoreBranchDialog,
  openShareDialog,
  timeAgo,
} from "../views/hive-views.js";
import { issueLink, issueTitle, relativeTime } from "../plugin/comments.js";

const ROLES = ["observer", "reviewer", "designer", "manager", "admin"];
const main = () => document.getElementById("main");
let me = null;
// A message for the next section shown (after a redirect within the page).
let notice = null;

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
const openURL = (projectId, branch) =>
  `/fontoverview.html?project=${encodeURIComponent(branch ? `${projectId}@${branch}` : projectId)}`;
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
      el("span", { class: "comment-count", "data-comments": p.id }),
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
          el("button", {
            class: "danger",
            onclick: () => deleteForGood([p.id]),
          }, ["Delete"]),
        ])
      );
    }
    details.append(
      el("div", { class: "row", style: "margin:0.5em 0" }, [
        el("span", { class: "grow" }),
        el("button", {
          class: "danger",
          onclick: () => deleteForGood(trashed.map((p) => p.id)),
        }, ["Empty the trash"]),
      ])
    );
    content.push(el("h3", {}, [""]), details);
  }
  main().replaceChildren(...content);
  showCommentCounts(projects);
}

// "3 open comments" on each card, filled in once the cards are shown (one
// small request per project; a project without a font yet has none).
async function showCommentCounts(projects) {
  await Promise.all(
    projects.map(async (p) => {
      try {
        const { open } = await hiveCall(`/api/hive/projects/${encodeURIComponent(p.id)}/comments/summary`);
        const slot = document.querySelector(`[data-comments="${CSS.escape(p.id)}"]`);
        if (slot && open) {
          slot.textContent = `${open} open comment${open === 1 ? "" : "s"}`;
          slot.title = "See them";
          slot.onclick = (event) => {
            event.preventDefault();
            location.hash = projectHash(p.id) + "/comments";
          };
        }
      } catch (error) {
        // no count: nothing shown
      }
    })
  );
}

// Deleting for good, from the trash only: hive-api forgets the projects, then
// the Fontra server removes their repositories (it also does so on its own
// every few minutes). Only the nightly backups keep them after that.
async function deleteForGood(ids, then = route) {
  const what = ids.length === 1 ? ids[0] : `the ${ids.length} projects in the trash`;
  if (!confirm(`Delete ${what} for good? The font and its whole history will be deleted. This cannot be undone.`)) return false;
  try {
    for (const id of ids) await call(`${hiveApiProjectPath(id)}/permanently`, undefined, "DELETE");
  } catch (error) {
    alert(error.message);
  }
  try {
    await call("/api/hive/sweep-deleted");
  } catch (error) {
    // the server's own sweep will remove them
  }
  then();
  return true;
}

async function newProjectSection() {
  const { organizations } = await get("/api/me/organizations");
  const owners = [me.username, ...organizations.filter((o) => o.role === "owner").map((o) => o.login)];
  const owner = el("select", { name: "owner" }, owners.map((o) => el("option", { value: o }, [o === me.username ? `${o} (you)` : o])));
  const name = el("input", { name: "name", required: true, maxlength: 100, pattern: "[A-Za-z0-9._\\-]+" });
  const description = el("input", { name: "description", maxlength: 300 });
  const empty = el("input", { type: "radio", name: "start", value: "empty", checked: true });
  const fromFont = el("input", { type: "radio", name: "start", value: "font" });
  const fromGit = el("input", { type: "radio", name: "start", value: "git" });
  const { input: file, bar, elements: fontFields } = importFields();
  const fontBox = el("div", { class: "hidden" }, fontFields);
  const toggle = () => fontBox.classList.toggle("hidden", !fromFont.checked);
  empty.addEventListener("change", toggle);
  fromFont.addEventListener("change", toggle);
  fromGit.addEventListener("change", toggle);
  const status = el("p", { class: "note" });
  const f = form(
    [
      field("Owner", owner, organizations.some((o) => o.role === "owner") ? "you, or an organization you own" : null),
      field("Name", name, "letters, digits, '-', '_' and '.'"),
      field("Description", description, "optional"),
      el("div", { class: "choices" }, [
        el("label", {}, [empty, "Start a new font (with a basic Latin glyph set to draw)"]),
        el("label", {}, [fromFont, "Import a font"]),
        el("label", {}, [fromGit, "Connect to an existing Git repository (GitHub…)"]),
      ]),
      fontBox,
      status,
    ],
    "Create the project",
    async () => {
      if (fromFont.checked && !file.files[0]) throw new Error("Choose a font file, or start a new font.");
      const { project } = await call("/api/projects", {
        name: name.value.trim(),
        owner: owner.value,
        description: description.value.trim(),
      });
      if (fromGit.checked) {
        // The project's page connects the repository and pulls its font.
        notice = "Connect the repository below: its font becomes the project's.";
        location.hash = projectHash(project.id);
        return;
      }
      if (fromFont.checked) {
        try {
          await importWithProgress(project.id, file.files[0], bar, status);
        } catch (error) {
          // The project exists but has no font: its page says so and offers
          // to import again or start empty (it does not open on an empty font).
          notice = `The project was created, but the font could not be imported: ${error.message}`;
          location.hash = projectHash(project.id);
          return;
        }
      }
      location.href = openURL(project.id);
    }
  );
  main().replaceChildren(el("h2", {}, ["New project"]), el("div", { class: "panel" }, [f]));
  name.focus();
}

// The Fontra server's routes answer errors in plain text.
async function hiveCall(path, method = "GET") {
  const response = await fetch(path, { method, credentials: "same-origin" });
  if (!response.ok) throw new Error((await response.text()) || `Error ${response.status}`);
  return response.json();
}

// The project's branches: open one, delete one (managers, or its maker),
// make one. Switching branch from the editor is the pill next to the name.
function branchesPanel(project, data) {
  const path = `/api/hive/projects/${encodeURIComponent(project.id)}/branches`;
  const error = el("div", { class: "error" });
  const rows = data.branches.map((b) => {
    const mayDelete = !b.isDefault && (data.can.delete || (data.can.create && b.createdBy === data.you));
    const madeBy = b.createdByName ? ` · made by ${b.createdByName}` : "";
    return el("tr", { "data-branch": b.name }, [
      el("td", {}, [
        el("b", {}, [b.name]),
        el("small", {}, [
          `${compareWithDefault(b, data.default)} · changed ${timeAgo(b.time)} by ${b.author}${madeBy}`,
        ]),
      ]),
      el("td", {}, [
        !b.isDefault && data.can.merge && b.ahead
          ? el("button", {
              class: "secondary",
              title: `Merge ${b.name} into ${data.default}`,
              onclick: () => openMergeDialog(project.id, {
                from: b.name,
                into: data.default,
                onDone: () => route(),
              }),
            }, ["Merge…"])
          : null,
      ]),
      el("td", {}, [el("a", { href: openURL(project.id, b.isDefault ? null : b.name) }, [
        el("button", { class: "secondary" }, ["Open"]),
      ])]),
      el("td", {}, [
        mayDelete
          ? el("button", {
              class: "danger",
              disabled: b.open,
              title: b.open ? "Someone has this branch open" : "",
              onclick: async () => {
                const kept = b.ahead
                  ? ` Its ${b.ahead} changes not in ${data.default} stay in the history, as “archive/${b.name}”.`
                  : "";
                if (!confirm(`Delete the branch “${b.name}”?${kept}`)) return;
                try {
                  await hiveCall(`${path}?${new URLSearchParams({ branch: b.name })}`, "DELETE");
                  route();
                } catch (e) {
                  error.textContent = e.message;
                }
              },
            }, ["Delete"])
          : null,
      ]),
    ]);
  });
  const archived = data.archived || [];
  const archivedRows = archived.map((a) =>
    el("tr", { "data-tag": a.tag }, [
      el("td", {}, [
        a.name,
        el("small", {}, [
          `deleted ${timeAgo(a.deleted)} by ${a.deletedBy}` +
            (a.ahead ? ` · ${a.ahead} changes not in ${data.default}` : ""),
        ]),
      ]),
      el("td", {}, [
        mayRestore(a, data)
          ? el("button", {
              class: "secondary",
              onclick: () => openRestoreBranchDialog(project.id, a, {
                defaultBranch: data.default,
                onRestored: () => route(),
              }),
            }, ["Restore"])
          : null,
      ]),
    ])
  );
  const panel = el("div", { class: "panel" }, [
    el("h3", { style: "margin-top:0" }, ["Branches"]),
    el("p", { class: "note" }, [
      `Copies of the font to try things out without changing ${data.default}; their changes can be merged back later.`,
    ]),
    el("table", { class: "people branches" }, rows),
    archived.length
      ? el("details", { class: "archived" }, [
          el("summary", {}, [`Deleted branches (${archived.length})`]),
          el("table", { class: "people archived" }, archivedRows),
        ])
      : null,
    error,
  ]);
  if (data.can.create) {
    const name = el("input", { maxlength: 100, placeholder: "e.g. bold-extension", autocomplete: "off" });
    const from = el("select", {}, data.branches.map((b) => el("option", { value: b.name }, [b.name])));
    panel.append(
      el("h3", {}, ["New branch"]),
      form([field("Name", name), field("Start from", from)], "Create", async () => {
        const problem = branchNameProblem(name.value.trim());
        if (problem) throw new Error(problem);
        const query = new URLSearchParams({ name: name.value.trim(), from: from.value });
        await hiveCall(`${path}?${query}`, "POST");
        route();
      })
    );
  }
  return panel;
}

// --- remote git repository (GitHub…) --------------------------------------------------
// Configured in hive-api (/api/github/…, /api/projects/…/remote); pull, push and
// status are the Fontra server's (/api/hive/projects/…/remote…).

const GITHUB_NOTICES = {
  connected: "GitHub is connected: choose the repository below.",
  requested: "The installation was sent to an owner of the GitHub organization for approval. Come back here once they have approved it.",
  cancelled: "GitHub was not connected.",
  failed: "GitHub did not complete the connection. Please try again.",
  "wrong-account": "That GitHub connection was started from another Hive account.",
  restart: "Start the GitHub connection from a project's settings in Hive.",
};

// The trip to GitHub runs in a tab of its own (GitHub does not always send
// people back: after a change of repositories on its own settings page, for
// one). This page updates when that tab says it is done, or when one comes
// back to it.
let githubTripPending = false;

export function openGitHubTab(url) {
  githubTripPending = true;
  const tab = window.open(url, "_blank");
  if (!tab) location.href = url; // pop-up blocked: the same tab, then back here
}

export function listenForGitHub() {
  window.addEventListener("message", (event) => {
    if (event.origin !== location.origin || event.data?.hive !== "github") return;
    githubTripPending = false;
    notice = GITHUB_NOTICES[event.data.result] || null;
    route();
  });
  window.addEventListener("focus", () => {
    if (!githubTripPending) return;
    githubTripPending = false;
    route();
  });
}

// The notice GitHub's return left in the address (?github=…), once.
export function takeGitHubNotice() {
  const params = new URLSearchParams(location.search);
  const code = params.get("github");
  if (!code) return null;
  params.delete("github");
  params.delete("project");
  const query = params.toString();
  history.replaceState(null, "", location.pathname + (query ? `?${query}` : "") + location.hash);
  return GITHUB_NOTICES[code] || null;
}

// The Fontra server's remote routes: errors are {message} or text.
async function remoteCall(path, method = "GET", body) {
  const response = await fetch(path, {
    method,
    credentials: "same-origin",
    headers: body !== undefined ? { "Content-Type": "application/json" } : {},
    body: body !== undefined ? JSON.stringify(body) : undefined,
  });
  const text = await response.text();
  let data = null;
  try {
    data = text ? JSON.parse(text) : null;
  } catch (error) {
    data = null;
  }
  if (!response.ok) {
    const error = new Error(data?.message || text || `Error ${response.status}`);
    error.code = data?.error;
    error.status = response.status;
    throw error;
  }
  return data;
}

// What a push to a UFO/designspace repository does not send (the .fontra
// files other than glyphs, glyph-info.csv and kerning.csv).
export function notSentToUFO(pending) {
  return pending.filter(
    (p) => !(p.startsWith("glyphs/") && p.endsWith(".json")) && p !== "kerning.csv" && p !== "glyph-info.csv"
  );
}

// A button at work: disabled (no second click), a spinner by its label, the
// panel aria-busy. work(step) runs; step("…") changes the label (the server
// gives no percentage, so the stages are named). Everything comes back as it
// was afterwards, after an error too.
export async function busy(button, label, work, panel = button.closest(".panel")) {
  const before = [...button.childNodes];
  const wasDisabled = button.disabled;
  const text = el("span", {}, [label]);
  button.disabled = true;
  button.classList.add("busy");
  button.replaceChildren(el("span", { class: "spinner", "aria-hidden": "true" }), text);
  panel?.setAttribute("aria-busy", "true");
  try {
    return await work((next) => {
      text.textContent = next;
    });
  } finally {
    button.replaceChildren(...before);
    button.classList.remove("busy");
    button.disabled = wasDisabled;
    panel?.removeAttribute("aria-busy");
  }
}

// The "Font in the repository" menu: what hive-api found in a branch
// (.fontra first, then designspaces, then UFOs; "" = a .fontra package at the
// root of the repository).
const OTHER_PATH = "*other*";

export function fontLabel(path) {
  const lower = path.toLowerCase();
  if (path === "") return ".fontra package at the root (recommended)";
  if (lower.endsWith(".fontra")) return `${path} (recommended)`;
  return `${path} (beta)`;
}

function fontsURL(fullName, branch) {
  const [owner, repo] = fullName.split("/").map(encodeURIComponent);
  return `/api/github/repositories/${owner}/${repo}/fonts?${new URLSearchParams({ branch })}`;
}

function remoteSummary(status, defaultBranch) {
  if (status.unmerged) {
    return `The changes pulled from the repository are not merged into ${defaultBranch} yet.`;
  }
  const parts = [];
  if (status.remoteMoved) parts.push("The repository has new commits: pull them.");
  if (status.pendingCount) {
    parts.push(`${status.pendingCount} file${status.pendingCount === 1 ? "" : "s"} changed in Hive since the last sync.`);
  }
  return parts.join(" ") || "Up to date.";
}

async function remoteConnectForm(project, panel, withFont) {
  const projectPath = hiveApiProjectPath(project.id);
  let github = null;
  try {
    github = await get("/api/github/status");
  } catch (error) {
    github = null;
  }
  const back = location.pathname + location.hash;
  const connectURL = (mode) =>
    "/api/github/connect?" +
    new URLSearchParams({ project: project.id, next: back, tab: "1", ...(mode ? { mode } : {}) });
  const tripLink = (label, mode) =>
    el("a", {
      href: connectURL(mode),
      onclick: (event) => {
        event.preventDefault();
        openGitHubTab(connectURL(mode));
      },
    }, [label]);
  const children = [];
  if (!github || !github.configured) {
    children.push(el("p", { class: "note" }, ["GitHub is not set up on this server."]));
  } else if (!github.connected) {
    children.push(
      el("p", { class: "note" }, [
        "Connect GitHub, then choose which repositories Hive may use. Only those, and only to read and write their contents.",
      ]),
      el("div", { class: "row" }, [
        el("button", { onclick: () => openGitHubTab(connectURL()) }, ["Connect GitHub"]),
      ]),
      el("p", { class: "note" }, [
        "GitHub opens in a new tab; this page updates when you come back. Already installed the app? ",
        tripLink("Sign in to GitHub", "authorize"),
        ".",
      ])
    );
  } else {
    let repos = [];
    let problem = null;
    try {
      repos = (await get("/api/github/repositories")).repositories;
    } catch (error) {
      problem = error.message;
    }
    const choice = el("select", { "aria-label": "Repository" }, repos.map((r) =>
      el("option", { value: r.fullName }, [r.fullName + (r.private ? " (private)" : "")])
    ));
    children.push(
      el("p", { class: "note" }, [
        `Signed in to GitHub as ${github.login}. `,
        tripLink("Add or remove repositories"),
        " · ",
        tripLink("Sign in to GitHub again", "authorize"),
      ])
    );
    if (problem) children.push(el("div", { class: "error" }, [problem]));
    if (repos.length) {
      children.push(githubConnectForm(project, repos, choice, withFont));
    } else if (!problem) {
      children.push(el("p", { class: "note" }, ["No repository yet: add one to the Fontra Hive app on GitHub."]));
    }
  }
  // Any other git host, by address and token.
  const url = el("input", { type: "url", placeholder: "https://gitlab.com/you/your-font.git", maxlength: 300 });
  const token = el("input", { type: "password", autocomplete: "off", maxlength: 300 });
  const otherPath = el("input", { placeholder: "e.g. sources/MyFont.designspace", maxlength: 300, autocomplete: "off" });
  const otherBranch = el("input", { placeholder: "main", maxlength: 100, autocomplete: "off" });
  children.push(
    el("details", { class: "other-host" }, [
      el("summary", {}, ["Another git host (GitLab…)"]),
      form([
        field("Repository address", url),
        field("Access token", token, "With read and write access to that repository only. Kept encrypted."),
        field("Branch", otherBranch),
        field("Font in the repository", otherPath),
      ], "Connect this repository", (f) =>
        connecting(f, "the repository", async () => {
          await call(`${projectPath}/remote`, {
            provider: "git",
            url: url.value.trim(),
            token: token.value.trim(),
            branch: otherBranch.value.trim(),
            path: otherPath.value.trim(),
          }, "PUT");
        }, project, withFont)
      ),
    ])
  );
  panel.append(...children);
}

// Connecting: save the connection, then the first pull (a shallow fetch, and
// for a UFO a conversion: several seconds on a big repository).
function connecting(f, from, save, project, withFont) {
  const button = f.querySelector("button[type=submit]");
  return busy(button, "Saving the connection…", async (step) => {
    await save();
    step(`Fetching the font from ${from}…`);
    await firstPull(project, withFont);
  });
}

// GitHub: repository, branch (its default one to start with) and a menu of
// the fonts hive-api finds in that branch; "Other path…" for anything else.
function githubConnectForm(project, repos, choice, withFont) {
  const projectPath = hiveApiProjectPath(project.id);
  const branch = el("input", { placeholder: "the repository's default branch", maxlength: 100, autocomplete: "off" });
  const fonts = el("select", { "aria-label": "Font in the repository" });
  const path = el("input", {
    class: "hidden",
    "aria-label": "Path of the font in the repository",
    placeholder: "e.g. sources/MyFont.designspace",
    maxlength: 300,
    autocomplete: "off",
  });
  const fontsNote = el("small", { class: "fonts-note" });
  // Nothing found yet (a push still on its way, say): look again on demand,
  // or when one comes back to this tab.
  const again = el("a", {
    href: "#",
    class: "look-again",
    onclick: (event) => {
      event.preventDefault();
      lookForFonts();
    },
  }, ["Look again"]);
  let nothingFound = false;
  const fontsField = el("label", {}, [
    "Font in the repository",
    el("small", {}, [".fontra is fully supported. UFO and designspace are in beta: glyphs and kerning only."]),
    fonts,
    path,
    fontsNote,
  ]);
  let search = 0; // the latest search; older answers are dropped
  let ready = false;
  const f = form([field("Repository", choice), field("Branch", branch), fontsField], "Connect this repository", () =>
    connecting(f, "GitHub", async () => {
      if (!ready) throw new Error("Choose the font in the repository first.");
      const manual = fonts.classList.contains("hidden") || fonts.value === OTHER_PATH;
      await call(`${projectPath}/remote`, {
        repository: choice.value,
        branch: branch.value.trim(),
        path: manual ? path.value.trim() : fonts.value,
      }, "PUT");
    }, project, withFont)
  );
  const submit = f.querySelector("button[type=submit]");
  const setReady = (value) => {
    ready = value;
    submit.disabled = !value;
  };
  const showPath = (shown) => path.classList.toggle("hidden", !shown);
  fonts.addEventListener("change", () => {
    showPath(fonts.value === OTHER_PATH);
    if (fonts.value === OTHER_PATH) path.focus();
  });

  let looked = null; // the repository and branch of the latest search
  async function lookForFonts() {
    const mine = ++search;
    const name = branch.value.trim();
    looked = `${choice.value}\n${name}`;
    fonts.classList.remove("hidden");
    fonts.disabled = true;
    fonts.replaceChildren(el("option", {}, ["Looking for fonts…"]));
    fontsNote.textContent = "";
    fontsNote.classList.remove("error");
    nothingFound = false;
    showPath(false);
    setReady(false);
    let answer;
    try {
      answer = await get(fontsURL(choice.value, name));
    } catch (error) {
      if (mine !== search) return;
      fonts.replaceChildren();
      fonts.classList.add("hidden");
      fontsNote.textContent = error.message;
      fontsNote.classList.add("error");
      // 409: GitHub must be connected again, nothing to connect; otherwise
      // the path can still be typed.
      showPath(error.status !== 409);
      setReady(error.status !== 409);
      return;
    }
    if (mine !== search) return;
    if (!answer.found) {
      fonts.replaceChildren(el("option", {}, [`No branch “${name}” in this repository`]));
      fontsNote.append(again);
      nothingFound = true;
      return;
    }
    if (!answer.fonts.length) {
      fonts.replaceChildren();
      fonts.classList.add("hidden");
      fontsNote.append("No font found in this branch: type its path, or push the font, then ", again, ".");
      nothingFound = true;
      showPath(true);
      setReady(true);
      return;
    }
    fonts.replaceChildren(
      ...answer.fonts.map((p) => el("option", { value: p }, [fontLabel(p)])),
      el("option", { value: OTHER_PATH }, ["Other path…"])
    );
    fonts.value = answer.fonts[0];
    fonts.disabled = false;
    setReady(true);
  }

  const repoChanged = () => {
    const repo = repos.find((r) => r.fullName === choice.value);
    branch.value = repo?.defaultBranch || "";
    lookForFonts();
  };
  choice.addEventListener("change", repoChanged);
  branch.addEventListener("change", () => {
    if (`${choice.value}\n${branch.value.trim()}` !== looked) lookForFonts();
  });
  window.addEventListener("focus", () => {
    if (nothingFound && f.isConnected && !path.value.trim()) lookForFonts();
  });
  repoChanged();
  return f;
}

function pullNotice(result, where) {
  if (!result.commit) return "Nothing new in the repository.";
  const glyphs = result.glyphs.length ? ` (${result.glyphs.length} glyphs changed)` : "";
  if (result.merged) return `The font of ${where} is now the project's${glyphs}.`;
  return `Pulled into ${result.branch}${glyphs}.`;
}

// Right after connecting: a project without its font takes the repository's
// at once; another one gets upstream/<branch> to review and merge.
async function firstPull(project, withFont) {
  const hive = `/api/hive/projects/${encodeURIComponent(project.id)}/remote`;
  if (!withFont || project.capabilities.includes("branch")) {
    try {
      const result = await remoteCall(`${hive}/pull`, "POST");
      notice = pullNotice(result, "the repository");
    } catch (error) {
      notice = `The repository is connected, but the first pull failed: ${error.message}`;
    }
  }
  route();
}

async function remotePanel(project, defaultBranch, withFont = true) {
  const caps = project.capabilities;
  const admin = caps.includes("administer");
  let remote = null;
  try {
    remote = (await get(`${hiveApiProjectPath(project.id)}/remote`)).remote;
  } catch (error) {
    remote = null;
  }
  if (!remote && !admin) return null;
  const panel = el("div", { class: "panel remote" }, [
    el("h3", { style: "margin-top:0" }, ["Git repository"]),
  ]);
  if (!remote) {
    panel.append(
      el("p", { class: "note" }, [
        "Keep the project in step with a repository on GitHub: pull what is pushed there, push what the team does here. ",
        el("a", { href: "/git", target: "_blank" }, ["How it works"]),
        ".",
      ])
    );
    await remoteConnectForm(project, panel, withFont);
    return panel;
  }

  const where = remote.repository || remote.url;
  const formatNote = remote.format === "ufo"
    ? " UFO and designspace are in beta: glyphs and kerning are sent, other font-level changes are not."
    : "";
  panel.append(
    el("p", {}, [
      el("b", {}, [where]),
      ` · branch ${remote.branch}` + (remote.path ? ` · ${remote.path}` : ""),
    ]),
    ...(formatNote ? [el("p", { class: "note" }, [formatNote.trim()])] : [])
  );
  const error = el("div", { class: "error" });
  const ok = el("div", { class: "ok" });
  const hive = `/api/hive/projects/${encodeURIComponent(project.id)}/remote`;

  if (remote.status !== "ok") {
    panel.append(el("div", { class: "error notice" }, [remote.statusReason || "The repository is disconnected."]));
  } else {
    const state = el("p", { class: "remote-state" }, ["Checking the repository…"]);
    const actions = el("div", { class: "row" });
    const skipped = el("p", { class: "note skipped" });
    panel.append(state, skipped, actions);
    try {
      const status = await remoteCall(hive);
      state.textContent = remoteSummary(status, defaultBranch);
      const notSent = remote.format === "ufo" ? notSentToUFO(status.pending) : [];
      if (notSent.length) {
        skipped.textContent = `Not sent to the repository: ${notSent.join(", ")}.`;
      }
      if (status.unmerged && caps.includes("merge")) {
        actions.append(
          el("button", {
            onclick: () => openMergeDialog(project.id, {
              from: status.upstreamBranch,
              into: defaultBranch,
              onDone: () => route(),
            }),
          }, [`Merge ${status.upstreamBranch}…`])
        );
      }
      if (caps.includes("branch")) {
        actions.append(
          el("button", {
            class: "secondary",
            onclick: async (event) => {
              error.textContent = ok.textContent = "";
              try {
                const result = await busy(event.currentTarget, "Pulling…", () => remoteCall(`${hive}/pull`, "POST"));
                notice = pullNotice(result, where);
                route();
              } catch (e) {
                error.textContent = e.message;
              }
            },
          }, ["Pull"])
        );
      }
      if (caps.includes("merge") && status.pendingCount) {
        const message = el("input", {
          maxlength: 300,
          placeholder: "Message (default: the latest snapshot's title)",
          "aria-label": "Commit message",
        });
        actions.append(
          message,
          el("button", {
            disabled: !status.canPush,
            title: status.canPush ? "" : "Pull and merge first",
            onclick: async (event) => {
              error.textContent = ok.textContent = "";
              const to = remote.provider === "github" ? "GitHub" : "the repository";
              try {
                const result = await busy(event.currentTarget, `Pushing to ${to}…`, () =>
                  remoteCall(`${hive}/push`, "POST", { message: message.value.trim() })
                );
                notice = result.pushed
                  ? `Pushed ${result.files.length} file${result.files.length === 1 ? "" : "s"} to ${where}.` +
                    (result.skipped.length ? ` Not sent: ${result.skipped.join(", ")}.` : "")
                  : "Nothing to push" + (result.skipped.length ? ` (not sent: ${result.skipped.join(", ")}).` : ".");
                route();
              } catch (e) {
                error.textContent = e.message;
              }
            },
          }, [`Push to ${remote.provider === "github" ? "GitHub" : "the repository"}`])
        );
      }
    } catch (e) {
      state.textContent = "";
      error.textContent = e.message;
    }
  }
  panel.append(error, ok);
  if (admin) {
    panel.append(
      el("div", { class: "row" }, [
        el("button", {
          class: "danger",
          onclick: async (event) => {
            if (!confirm(`Disconnect ${where}? Nothing is deleted, here or there.`)) return;
            error.textContent = "";
            try {
              await busy(event.currentTarget, "Disconnecting…", () =>
                call(`${hiveApiProjectPath(project.id)}/remote`, undefined, "DELETE")
              );
              route();
            } catch (e) {
              error.textContent = e.message;
            }
          },
        }, ["Disconnect"]),
      ])
    );
  }
  return panel;
}

// --- comments on glyphs -------------------------------------------------------------

async function loadComments(projectId) {
  try {
    return await hiveCall(`/api/hive/projects/${encodeURIComponent(projectId)}/comments`);
  } catch (error) {
    return null;
  }
}

function commentsPanel(project, data) {
  const open = data.issues.filter((i) => i.state === "open");
  const mine = data.you
    ? open.filter((i) => i.assignee?.username === data.you.username).length
    : 0;
  return el("div", { class: "panel comments-summary" }, [
    el("h3", { style: "margin-top:0" }, ["Comments"]),
    el("p", { class: "note" }, [
      data.issues.length
        ? `${open.length} open · ${data.issues.length - open.length} resolved` +
          (mine ? ` · ${mine} assigned to you` : "")
        : "No comments yet. In the editor, the Comment tool pins a note to a point of a glyph.",
    ]),
    data.issues.length
      ? el("a", { href: projectHash(project.id) + "/comments" }, [
          el("button", { class: "secondary" }, ["See all comments"]),
        ])
      : null,
  ]);
}

// Filters of the comments page, kept in the page's address after "?".
export function filterComments(issues, f, you) {
  const text = (f.q || "").trim().toLowerCase();
  return issues.filter((issue) => {
    if (f.state === "open" && issue.state !== "open") return false;
    if (f.state === "resolved" && issue.state !== "resolved") return false;
    if (f.glyph && issue.glyph !== f.glyph) return false;
    if (f.branch && issue.branch !== f.branch) return false;
    if (f.label && !(issue.labels || []).includes(f.label)) return false;
    const person = f.person === "me" ? you?.username : f.person;
    if (person) {
      const involved = [issue.author, issue.assignee, ...issue.messages.map((m) => m.author)];
      if (f.role === "assignee" ? issue.assignee?.username !== person : !involved.some((p) => p?.username === person)) {
        return false;
      }
    }
    if (text) {
      const haystack = [issue.glyph, `#${issue.number}`, issue.title, ...(issue.labels || []), ...issue.messages.map((m) => m.text)]
        .join("\n")
        .toLowerCase();
      if (!haystack.includes(text)) return false;
    }
    return true;
  });
}

async function commentsSection(projectId) {
  const data = await hiveCall(`/api/hive/projects/${encodeURIComponent(projectId)}/comments`);
  const issues = [...data.issues].sort((a, b) => b.number - a.number);
  const f = { state: "open", glyph: "", person: "", role: "involved", label: "", branch: "", q: "" };
  const unique = (values) => [...new Set(values.filter(Boolean))].sort((a, b) => a.localeCompare(b));
  const select = (key, label, options) => {
    const element = el("select", { "aria-label": label, "data-filter": key }, options.map(([value, text]) =>
      el("option", { value }, [text])
    ));
    element.value = f[key];
    element.addEventListener("change", () => {
      f[key] = element.value;
      render();
    });
    return element;
  };
  const people = new Map();
  for (const m of data.members || []) people.set(m.username, m.name || m.username);
  for (const issue of issues) {
    for (const p of [issue.author, issue.assignee, ...issue.messages.map((m) => m.author)]) {
      if (p && !people.has(p.username)) people.set(p.username, p.name || p.username);
    }
  }
  const search = el("input", { type: "search", placeholder: "Search", "aria-label": "Search", "data-filter": "q" });
  search.addEventListener("input", () => {
    f.q = search.value;
    render();
  });
  const filters = el("div", { class: "row comment-filters" }, [
    select("state", "State", [["open", "Open"], ["resolved", "Resolved"], ["all", "All"]]),
    select("glyph", "Glyph", [["", "Any glyph"], ...unique(issues.map((i) => i.glyph)).map((g) => [g, g])]),
    select("person", "Person", [
      ["", "Anyone"],
      ...(data.you ? [["me", "Me"]] : []),
      ...[...people.entries()].sort((a, b) => a[1].localeCompare(b[1])).map(([u, n]) => [u, n]),
    ]),
    select("role", "How", [["involved", "wrote or assigned"], ["assignee", "assigned"]]),
    select("label", "Label", [["", "Any label"], ...unique(issues.flatMap((i) => i.labels || [])).map((l) => [l, l])]),
    unique(issues.map((i) => i.branch)).length > 1
      ? select("branch", "Branch", [["", "Any branch"], ...unique(issues.map((i) => i.branch)).map((b) => [b, b])])
      : null,
    search,
  ]);
  const table = el("table", { class: "people comments" });
  const count = el("p", { class: "note" });
  const render = () => {
    const shown = filterComments(issues, f, data.you);
    count.textContent = `${shown.length} of ${issues.length} comment${issues.length === 1 ? "" : "s"}`;
    table.replaceChildren(
      ...shown.map((issue) => {
        const first = issue.messages[0] || {};
        const replies = issue.messages.length - 1;
        return el("tr", { "data-number": issue.number, class: issue.state }, [
          el("td", { class: "number" }, [`#${issue.number}`]),
          el("td", {}, [
            el("b", {}, [issue.glyph]),
            el("small", {}, [issue.source?.name || ""]),
          ]),
          el("td", { class: "text" }, [
            el("span", { title: issue.title ? first.text || "" : "" }, [issueTitle(issue)]),
            el("small", {}, [
              [
                `${first.author?.name || first.author?.username || "?"}, ${relativeTime(issue.created)}`,
                replies ? `${replies} repl${replies > 1 ? "ies" : "y"}` : null,
                issue.state === "resolved"
                  ? `resolved by ${issue.resolved?.by?.name || issue.resolved?.by?.username || "?"}`
                  : null,
                issue.assignee ? `→ ${issue.assignee.name || issue.assignee.username}` : null,
              ].filter(Boolean).join(" · "),
            ]),
          ]),
          el("td", { class: "labels" }, (issue.labels || []).map((l) => el("span", { class: "label-chip" }, [l]))),
          el("td", {}, [
            el("a", { href: issueLink(projectId, issue), title: `Open ${issue.glyph} in the editor, with this comment` }, [
              el("button", { class: "secondary" }, ["Open glyph"]),
            ]),
          ]),
        ]);
      })
    );
  };
  main().replaceChildren(
    el("h2", {}, [
      el("span", { class: "grow" }, [`${projectId} — comments`]),
      el("a", { href: projectHash(projectId) }, [el("button", { class: "secondary" }, ["Back to the project"])]),
    ]),
    el("div", { class: "panel" }, [filters, count, table]),
  );
  render();
}

// Whether the project has its font yet (a repository on the Fontra server).
// When the server cannot say, assume it does: opening then works as before.
async function hasFont(projectId) {
  try {
    return (await get(`/api/hive/projects/${encodeURIComponent(projectId)}/repository`)).exists !== false;
  } catch (error) {
    return true;
  }
}

async function projectSection(projectId) {
  const base = hiveApiProjectPath(projectId);
  const { project } = await get(base);
  const admin = project.capabilities.includes("administer");
  const withFont = project.trashed || (await hasFont(project.id));
  const message = notice;
  notice = null;
  const content = [
    el("h2", {}, [
      el("span", { class: "grow" }, [project.id, project.trashed ? " (in the trash)" : ""]),
      project.trashed || !withFont ? null : el("a", { href: openURL(project.id) }, [el("button", {}, ["Open"])]),
    ]),
    el("p", { class: "note" }, [`Your role: ${project.role}.`]),
  ];
  if (message) content.splice(1, 0, el("div", { class: "error notice" }, [message]));
  if (!withFont) {
    content.push(
      el("div", { class: "panel no-font" }, [
        el("h3", { style: "margin-top:0" }, ["No font yet"]),
        admin
          ? el("p", { class: "note" }, ["Import a font below, connect a Git repository, or start a new font."])
          : el("p", { class: "note" }, ["The project's admins have not added its font yet."]),
        admin
          ? el("button", {
              onclick: async (event) => {
                event.target.disabled = true;
                try {
                  // Any read of the project creates it, with an empty font.
                  await get(`/api/hive/projects/${encodeURIComponent(project.id)}/head`);
                  location.href = openURL(project.id);
                } finally {
                  event.target.disabled = false;
                }
              },
            }, ["Start a new font"])
          : null,
      ])
    );
  }

  // The Git repository: right under "No font yet" for a project without
  // its font (it can bring one), after the branches otherwise.
  const remote = project.trashed
    ? null
    : await remotePanel(project, project.defaultBranch || "main", withFont);
  if (remote && !withFont) content.push(remote);

  // People
  content.push(
    el("div", { class: "panel" }, [
      el("h3", { style: "margin-top:0" }, ["People"]),
      el("p", { class: "note" }, ["Who has access, with which role; invite people by username or email."]),
      el("button", { onclick: () => openShareDialog(project.id) }, ["Manage people…"]),
    ])
  );

  let branches = null;
  if (withFont && !project.trashed) {
    try {
      branches = await hiveCall(`/api/hive/projects/${encodeURIComponent(project.id)}/branches`);
    } catch (error) {
      branches = null;
    }
    if (branches) content.push(branchesPanel(project, branches));
    if (remote) content.push(remote);
    const comments = await loadComments(project.id);
    if (comments) content.push(commentsPanel(project, comments));
  }

  // Download: the sources, or fonts built on the server (managers, admins).
  if (withFont && !project.trashed && project.capabilities.includes("export")) {
    let formats = [];
    try {
      formats = (await get("/api/hive/export-formats")).formats;
    } catch (error) {
      formats = [];
    }
    const defaultBranch = branches?.default || project.defaultBranch || "main";
    const branchChoice = branches && branches.branches.length > 1
      ? el("select", { "aria-label": "Branch to download" }, branches.branches.map((b) =>
          el("option", { value: b.name }, [b.name])))
      : null;
    if (formats.length) {
      content.push(
        el("div", { class: "panel" }, [
          el("h3", { style: "margin-top:0" }, ["Download"]),
          branchChoice
            ? el("div", { class: "row" }, [el("span", {}, ["The latest version of the branch"]), branchChoice])
            : el("p", { class: "note" }, [`The latest version of the ${defaultBranch} branch.`]),
          el("div", { class: "row downloads" }, formats.map(({ format, label }) =>
            el("button", {
              class: "secondary",
              onclick: async (event) => {
                event.target.disabled = true;
                try {
                  const branch = branchChoice ? branchChoice.value : defaultBranch;
                  await exportDownload({ name: project.id, branch }, format);
                } finally {
                  event.target.disabled = false;
                }
              },
            }, [label])
          )),
        ])
      );
    }
  }

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
          if (!withFont) {
            notice = null;
            route(); // the project has its font now: "Open" comes back
          }
          return `Imported ${file.files[0].name}.`;
        }),
      ])
    );
    content.push(
      el("div", { class: "panel" }, [
        el("h3", { style: "margin-top:0" }, [project.trashed ? "In the trash" : "Trash"]),
        project.trashed
          ? el("div", { class: "row" }, [
              el("button", { onclick: async () => { await call(`${base}/restore`); route(); } }, ["Restore the project"]),
              el("button", {
                class: "danger",
                onclick: () => deleteForGood([project.id], () => (location.hash = "")),
              }, ["Delete for good"]),
            ])
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
        el("p", { class: "note" }, ["People who already have a Hive account, by the address of their account."]),
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
    const membersOnly = el("input", { type: "checkbox", name: "membersOnly", checked: !!org.membersOnly });
    content.push(
      el("div", { class: "panel" }, [
        el("h3", { style: "margin-top:0" }, ["Settings"]),
        form([
          field("Full name", name),
          field("Members' role on every project", baseRole, "owners are admins of every project"),
          el("label", { class: "check" }, [membersOnly, "Share our projects with our members only (no outside collaborators)"]),
        ], "Save", async () => {
          await call(base, { name: name.value.trim(), baseRole: baseRole.value, membersOnly: membersOnly.checked }, "PATCH");
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
      if (parts.length === 4 && parts[3] === "comments") {
        return { section: "projects", view: "comments", project: `${parts[1]}/${parts[2]}` };
      }
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
    else if (where.view === "comments") await commentsSection(where.project);
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
  listenForGitHub();
  notice = takeGitHubNotice() || notice;
  await Promise.all([showInvitations(), route()]);
}

if (!window.__hiveHomeNoAutoStart) start();
