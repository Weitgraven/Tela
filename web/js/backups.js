import { ref, reactive, computed, onMounted, onUnmounted } from "../vendor/vue.esm-browser.prod.js";
import { api, session, formatBytes } from "./core.js";

const TYPE_LABEL = { local: "Local folder", s3: "S3-compatible", b2: "Backblaze B2", sftp: "SFTP" };
const WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];
const STATUS_LABEL = { ok: "Succeeded", partial: "Partly failed", failed: "Failed", running: "Running", queued: "Starting" };
const STATUS_STATE = { ok: "active", partial: "warning", failed: "failed", running: "busy", queued: "busy" };

function formatDateTime(value) {
  if (!value) return "";
  const date = typeof value === "number" ? new Date(value * 1000) : new Date(value);
  return date.toLocaleString(undefined, {
    year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
  });
}

function scheduleText(s) {
  if (!s || s.frequency === "manual") return "Only when started by hand";
  if (s.frequency === "weekly") return `Every ${WEEKDAYS[s.weekday]} at ${s.time}`;
  return `Daily at ${s.time}`;
}

function keepText(k) {
  const parts = [];
  if (k.daily) parts.push(`${k.daily} daily`);
  if (k.weekly) parts.push(`${k.weekly} weekly`);
  if (k.monthly) parts.push(`${k.monthly} monthly`);
  return parts.length ? `Keeps ${parts.join(", ")}` : "Keeps every backup";
}

function duration(job) {
  if (!job.started_at) return "";
  const seconds = (job.finished_at || Math.floor(Date.now() / 1000)) - job.started_at;
  if (seconds < 60) return `${seconds}s`;
  return `${Math.floor(seconds / 60)}m ${seconds % 60}s`;
}

async function copy(text, flag) {
  try {
    await navigator.clipboard.writeText(text);
    flag.value = true;
    setTimeout(() => (flag.value = false), 1500);
  } catch {
    // Clipboard unavailable; the text is selectable anyway.
  }
}

// ---------------------------------------------------------------------------
// Destination form (add or edit)
// ---------------------------------------------------------------------------

function blankDestination() {
  return {
    name: "", type: "s3", path: "/backup/tela", endpoint: "", bucket: "", prefix: "", region: "",
    access_key: "", secret_key: "", account_id: "", account_key: "", host: "", port: 22,
    user: "", repo_password: "", notify_email: "",
    schedule: { frequency: "daily", time: "03:00", weekday: 6 },
    keep: { daily: 7, weekly: 4, monthly: 6 },
    include: { files: true, databases: true, mail: true, server: true },
  };
}

const DestinationForm = {
  props: ["editing"],
  emits: ["done", "cancel"],
  setup(props, { emit }) {
    const isEdit = !!props.editing;
    const form = reactive(blankDestination());
    if (isEdit) {
      const d = props.editing;
      Object.assign(form, {
        name: d.name, type: d.type, notify_email: d.notify_email || "",
        access_key: d.access_key || "", account_id: d.account_id || "",
        schedule: { ...d.schedule }, keep: { ...d.keep }, include: { ...d.include },
      });
    }
    const busy = ref(false);
    const error = ref("");
    const publicKey = ref("");
    const copied = ref(false);

    async function loadKey() {
      if (form.type !== "sftp" || publicKey.value) return;
      try {
        publicKey.value = (await api("/api/backups/ssh-key", { method: "POST" })).public_key;
      } catch (err) {
        error.value = err.message;
      }
    }

    async function submit() {
      busy.value = true;
      error.value = "";
      try {
        let result;
        if (isEdit) {
          const body = {
            name: form.name, schedule: form.schedule, keep: form.keep, include: form.include,
            notify_email: form.notify_email,
          };
          if (form.type === "s3" && form.secret_key) Object.assign(body, { access_key: form.access_key, secret_key: form.secret_key });
          if (form.type === "b2" && form.account_key) Object.assign(body, { account_id: form.account_id, account_key: form.account_key });
          result = await api(`/api/backups/destinations/${props.editing.id}`, { method: "PUT", body });
        } else {
          const fields = {
            local: ["path"], s3: ["endpoint", "bucket", "prefix", "region", "access_key", "secret_key"],
            b2: ["bucket", "prefix", "account_id", "account_key"], sftp: ["host", "port", "user", "path"],
          }[form.type];
          const body = {
            name: form.name, type: form.type, schedule: form.schedule, keep: form.keep,
            include: form.include, notify_email: form.notify_email || null,
            repo_password: form.repo_password || null,
          };
          for (const f of fields) body[f] = form[f] === "" ? null : form[f];
          if (form.type === "sftp") body.port = Number(form.port) || 22;
          result = await api("/api/backups/destinations", { method: "POST", body });
        }
        emit("done", result);
      } catch (err) {
        error.value = err.message;
      } finally {
        busy.value = false;
      }
    }

    return { form, busy, error, isEdit, publicKey, copied, loadKey, submit, TYPE_LABEL, WEEKDAYS,
      copyKey: () => copy(publicKey.value, copied) };
  },
  template: `
    <form class="panel form-panel backup-form" @submit.prevent="submit">
      <h2>{{ isEdit ? "Edit " + editing.name : "Add a backup destination" }}</h2>
      <p v-if="error" class="alert" role="alert">{{ error }}</p>

      <div class="field">
        <label for="dest-name">Name</label>
        <input id="dest-name" v-model.trim="form.name" placeholder="Backblaze, NAS at the office…" required>
      </div>

      <fieldset v-if="!isEdit" class="choice">
        <legend>Where to store the backups</legend>
        <label v-for="(label, t) in TYPE_LABEL" :key="t" class="check">
          <input type="radio" v-model="form.type" :value="t" @change="loadKey">
          <span>{{ label }}</span>
        </label>
      </fieldset>

      <template v-if="!isEdit">
        <div v-if="form.type === 'local'" class="field">
          <label for="dest-path">Folder on this server</label>
          <input id="dest-path" v-model.trim="form.path" spellcheck="false" required>
          <p class="muted hint">Only protects against mistakes, not against losing the server. Best for a second disk.</p>
        </div>

        <div v-if="form.type === 's3'" class="form-grid form-grid-even">
          <div class="field span-2">
            <label for="dest-endpoint">Endpoint</label>
            <input id="dest-endpoint" v-model.trim="form.endpoint" placeholder="s3.eu-central-003.backblazeb2.com" spellcheck="false">
            <p class="muted hint">Leave empty for Amazon S3. Works with Backblaze B2, Wasabi, Cloudflare R2, MinIO and others.</p>
          </div>
          <div class="field"><label for="dest-bucket">Bucket</label><input id="dest-bucket" v-model.trim="form.bucket" spellcheck="false" required></div>
          <div class="field"><label for="dest-prefix">Folder in the bucket <span class="muted">(optional)</span></label><input id="dest-prefix" v-model.trim="form.prefix" placeholder="tela" spellcheck="false"></div>
          <div class="field"><label for="dest-ak">Access key ID</label><input id="dest-ak" v-model.trim="form.access_key" autocomplete="off" spellcheck="false" required></div>
          <div class="field"><label for="dest-sk">Secret access key</label><input id="dest-sk" v-model.trim="form.secret_key" type="password" autocomplete="off" required></div>
          <div class="field"><label for="dest-region">Region <span class="muted">(if your provider needs it)</span></label><input id="dest-region" v-model.trim="form.region" placeholder="eu-central-1" spellcheck="false"></div>
        </div>

        <div v-if="form.type === 'b2'" class="form-grid form-grid-even">
          <div class="field"><label for="b2-bucket">Bucket</label><input id="b2-bucket" v-model.trim="form.bucket" spellcheck="false" required></div>
          <div class="field"><label for="b2-prefix">Folder in the bucket <span class="muted">(optional)</span></label><input id="b2-prefix" v-model.trim="form.prefix" placeholder="tela" spellcheck="false"></div>
          <div class="field"><label for="b2-id">Application key ID</label><input id="b2-id" v-model.trim="form.account_id" autocomplete="off" spellcheck="false" required></div>
          <div class="field"><label for="b2-key">Application key</label><input id="b2-key" v-model.trim="form.account_key" type="password" autocomplete="off" required></div>
          <p class="muted hint span-2">Tip: choosing "S3-compatible" with your B2 endpoint is usually faster.</p>
        </div>

        <template v-if="form.type === 'sftp'">
          <div class="form-grid sftp-grid">
            <div class="field"><label for="sftp-host">Server</label><input id="sftp-host" v-model.trim="form.host" placeholder="nas.example.com" spellcheck="false" required></div>
            <div class="field"><label for="sftp-port">Port</label><input id="sftp-port" v-model.number="form.port" type="number" min="1" max="65535"></div>
            <div class="field"><label for="sftp-user">User</label><input id="sftp-user" v-model.trim="form.user" autocapitalize="none" spellcheck="false" required></div>
            <div class="field"><label for="sftp-path">Folder</label><input id="sftp-path" v-model.trim="form.path" placeholder="backups/tela" spellcheck="false" required></div>
          </div>
          <div class="key-box">
            <p>Add this key to <code>~/.ssh/authorized_keys</code> of that user, then save:</p>
            <p v-if="!publicKey" class="loading">Creating key…</p>
            <div v-else class="key-row"><code class="secret">{{ publicKey }}</code>
              <button class="button button-quiet" type="button" @click="copyKey">{{ copied ? "Copied" : "Copy" }}</button></div>
          </div>
        </template>
      </template>

      <template v-else-if="form.type === 's3' || form.type === 'b2'">
        <details class="advanced">
          <summary>Change the access key</summary>
          <div class="form-grid form-grid-even" v-if="form.type === 's3'">
            <div class="field"><label for="e-ak">Access key ID</label><input id="e-ak" v-model.trim="form.access_key" autocomplete="off"></div>
            <div class="field"><label for="e-sk">New secret access key</label><input id="e-sk" v-model.trim="form.secret_key" type="password" autocomplete="off"></div>
          </div>
          <div class="form-grid form-grid-even" v-else>
            <div class="field"><label for="e-id">Application key ID</label><input id="e-id" v-model.trim="form.account_id" autocomplete="off"></div>
            <div class="field"><label for="e-key">New application key</label><input id="e-key" v-model.trim="form.account_key" type="password" autocomplete="off"></div>
          </div>
        </details>
      </template>

      <h3 class="form-section">When</h3>
      <div class="form-grid schedule-grid">
        <div class="field">
          <label for="sched-freq">Back up</label>
          <select id="sched-freq" v-model="form.schedule.frequency">
            <option value="daily">Every day</option>
            <option value="weekly">Every week</option>
            <option value="manual">Only by hand</option>
          </select>
        </div>
        <div class="field" v-if="form.schedule.frequency === 'weekly'">
          <label for="sched-day">On</label>
          <select id="sched-day" v-model.number="form.schedule.weekday">
            <option v-for="(d, i) in WEEKDAYS" :key="d" :value="i">{{ d }}</option>
          </select>
        </div>
        <div class="field" v-if="form.schedule.frequency !== 'manual'">
          <label for="sched-time">At</label>
          <input id="sched-time" v-model="form.schedule.time" type="time" required>
        </div>
      </div>

      <h3 class="form-section">How long to keep them</h3>
      <div class="form-grid keep-grid">
        <div class="field"><label for="keep-d">Daily</label><input id="keep-d" v-model.number="form.keep.daily" type="number" min="0" max="366"></div>
        <div class="field"><label for="keep-w">Weekly</label><input id="keep-w" v-model.number="form.keep.weekly" type="number" min="0" max="260"></div>
        <div class="field"><label for="keep-m">Monthly</label><input id="keep-m" v-model.number="form.keep.monthly" type="number" min="0" max="240"></div>
      </div>
      <p class="muted hint">Older backups are removed after each run. Set all three to 0 to keep everything.</p>

      <h3 class="form-section">What</h3>
      <div class="check-row">
        <label class="check"><input type="checkbox" v-model="form.include.files"><span>Website files</span></label>
        <label class="check"><input type="checkbox" v-model="form.include.databases"><span>Databases</span></label>
        <label class="check"><input type="checkbox" v-model="form.include.mail"><span>Email</span></label>
        <label class="check"><input type="checkbox" v-model="form.include.server"><span>Server configuration</span></label>
      </div>

      <div class="field">
        <label for="dest-notify">Email me when a backup fails <span class="muted">(optional)</span></label>
        <input id="dest-notify" v-model.trim="form.notify_email" type="email" placeholder="you@example.com">
      </div>

      <details v-if="!isEdit" class="advanced">
        <summary>Connect to an existing backup repository</summary>
        <div class="field">
          <label for="dest-repo-pw">Repository password</label>
          <input id="dest-repo-pw" v-model="form.repo_password" type="password" autocomplete="off">
          <p class="muted hint">For backups made by a previous Tela server at this same location. Leave empty to start a new repository.</p>
        </div>
      </details>

      <div class="actions">
        <button class="button" type="submit" :disabled="busy">{{ busy ? (isEdit ? "Saving…" : "Connecting…") : (isEdit ? "Save" : "Add destination") }}</button>
        <button class="button button-quiet" type="button" @click="$emit('cancel')" :disabled="busy">Cancel</button>
      </div>
    </form>
  `,
};

// ---------------------------------------------------------------------------
// Restore a website
// ---------------------------------------------------------------------------

const RestorePanel = {
  props: ["destinations", "sites"],
  emits: ["started"],
  setup(props, { emit }) {
    const domain = ref(props.sites[0] || "");
    const snapshots = ref(null);
    const loading = ref(false);
    const error = ref("");
    const chosen = ref(null);
    const parts = reactive({ files: true, databases: true, mail: false });
    const path = ref("");
    const confirm = ref(false);
    const busy = ref(false);

    async function load() {
      if (!domain.value) return;
      loading.value = true;
      error.value = "";
      chosen.value = null;
      try {
        snapshots.value = (await api(`/api/backups/sites/${domain.value}/snapshots`)).snapshots;
      } catch (err) {
        error.value = err.message;
        snapshots.value = null;
      } finally {
        loading.value = false;
      }
    }

    function choose(s) {
      chosen.value = s;
      confirm.value = false;
      Object.assign(parts, { files: true, databases: true, mail: false });
      path.value = "";
    }

    async function backupNow() {
      busy.value = true;
      error.value = "";
      try {
        const job = await api(`/api/backups/sites/${domain.value}/run`, { method: "POST", body: {} });
        emit("started", job);
      } catch (err) {
        error.value = err.message;
      } finally {
        busy.value = false;
      }
    }

    async function restore() {
      busy.value = true;
      error.value = "";
      try {
        const job = await api(`/api/backups/sites/${domain.value}/restore`, {
          method: "POST",
          body: {
            destination: chosen.value.destination, snapshot: chosen.value.full_id,
            files: parts.files, databases: parts.databases, mail: parts.mail,
            path: parts.files && path.value ? path.value : null,
          },
        });
        chosen.value = null;
        emit("started", job);
      } catch (err) {
        error.value = err.message;
      } finally {
        busy.value = false;
      }
    }

    // restic before 0.17 doesn't record sizes in its snapshot list.
    const hasSizes = computed(() => (snapshots.value || []).some((s) => s.size));
    return { domain, snapshots, loading, error, chosen, parts, path, confirm, busy, load, choose, hasSizes,
      restore, backupNow, formatDateTime, formatBytes };
  },
  template: `
    <section class="panel restore-panel">
      <div class="table-head"><h2 class="table-title">Restore a website</h2></div>
      <p v-if="!sites.length" class="muted table-empty">There are no websites to restore.</p>
      <template v-else>
        <form class="inline-form restore-pick" @submit.prevent="load">
          <div class="field">
            <label for="restore-site">Website</label>
            <select id="restore-site" v-model="domain" @change="snapshots = null; chosen = null">
              <option v-for="s in sites" :key="s" :value="s">{{ s }}</option>
            </select>
          </div>
          <button class="button button-secondary" type="submit" :disabled="loading">{{ loading ? "Loading…" : "Show backups" }}</button>
          <button class="button button-quiet" type="button" :disabled="busy || !destinations.length" @click="backupNow">Back up now</button>
        </form>
        <p v-if="error" class="alert restore-alert" role="alert">{{ error }}</p>

        <p v-if="snapshots && !snapshots.length" class="muted table-empty">No backups of {{ domain }} yet.</p>
        <table v-else-if="snapshots" class="table db-table">
          <thead><tr><th scope="col">Backup</th><th scope="col">Stored at</th><th scope="col" v-if="hasSizes">Size</th><th scope="col"><span class="visually-hidden">Actions</span></th></tr></thead>
          <tbody>
            <template v-for="s in snapshots" :key="s.destination + s.id">
              <tr class="static-row" :class="{ selected: chosen === s }">
                <td class="db-name">{{ formatDateTime(s.time) }}</td>
                <td>{{ s.destination_name }}</td>
                <td v-if="hasSizes">{{ s.size ? formatBytes(s.size) : "—" }}</td>
                <td class="row-actions"><button class="button button-quiet" type="button" @click="choose(s)">Restore…</button></td>
              </tr>
              <tr v-if="chosen === s" class="static-row restore-row">
                <td :colspan="hasSizes ? 4 : 3">
                  <form class="restore-form" @submit.prevent="confirm ? restore() : (confirm = true)">
                    <div class="check-row">
                      <label class="check"><input type="checkbox" v-model="parts.files" @change="confirm = false"><span>Files</span></label>
                      <label class="check"><input type="checkbox" v-model="parts.databases" @change="confirm = false"><span>Databases</span></label>
                      <label class="check" v-if="s.has_mail"><input type="checkbox" v-model="parts.mail" @change="confirm = false"><span>Email</span></label>
                    </div>
                    <div class="field" v-if="parts.files">
                      <label :for="'restore-path-' + s.id">Only this file or folder <span class="muted">(optional)</span></label>
                      <input :id="'restore-path-' + s.id" v-model.trim="path" placeholder="public_html/wp-config.php" spellcheck="false" @input="confirm = false">
                    </div>
                    <p class="muted restore-warning">
                      <template v-if="parts.files && !path">The website's files are replaced by this backup: files added since then are removed.</template>
                      <template v-if="parts.databases"> Its databases are replaced by the backed-up version.</template>
                      <template v-if="parts.mail"> Deleted email comes back; newer email is kept.</template>
                    </p>
                    <div class="actions">
                      <button class="button" :class="{ 'button-danger': confirm }" type="submit" :disabled="busy || !(parts.files || parts.databases || parts.mail)">
                        {{ confirm ? "Yes, restore " + formatDateTime(s.time) : "Restore" }}</button>
                      <button class="button button-quiet" type="button" @click="chosen = null">Cancel</button>
                    </div>
                  </form>
                </td>
              </tr>
            </template>
          </tbody>
        </table>
      </template>
    </section>
  `,
};

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export const BackupsView = {
  components: { DestinationForm, RestorePanel },
  setup() {
    const status = ref(null);
    const jobs = ref([]);
    const sites = ref([]);
    const error = ref("");
    const notice = ref("");
    const isAdmin = computed(() => session.user?.role === "admin");
    const formFor = ref(null); // null, "new", or a destination
    const created = ref(null); // { name, repo_password }
    const passwordShown = ref(null);
    const confirmDelete = ref(null);
    const openJob = ref(null);
    const busy = ref(false);
    const copied = ref(false);
    let timer = null;

    async function loadJobs() {
      jobs.value = (await api("/api/backups/jobs")).jobs;
      if (openJob.value) {
        openJob.value = await api(`/api/backups/jobs/${openJob.value.id}`);
      }
      schedulePoll();
    }

    async function load() {
      try {
        const [st, w] = await Promise.all([api("/api/backups/status"), api("/api/websites")]);
        status.value = st;
        sites.value = w.websites.map((s) => s.domain);
        await loadJobs();
        error.value = "";
      } catch (err) {
        error.value = err.message;
      }
    }

    function schedulePoll() {
      clearTimeout(timer);
      if (jobs.value.some((j) => j.status === "queued" || j.status === "running")) {
        timer = setTimeout(async () => {
          const before = jobs.value.filter((j) => j.status === "running" || j.status === "queued").length;
          await loadJobs().catch(() => {});
          const after = jobs.value.filter((j) => j.status === "running" || j.status === "queued").length;
          if (after < before) {
            status.value = await api("/api/backups/status").catch(() => status.value);
          }
        }, 3000);
      }
    }

    function formDone(result) {
      formFor.value = null;
      if (result.repo_password) created.value = { name: result.name, repo_password: result.repo_password };
      else notice.value = result.connected_existing ? `Connected to the existing backups at ${result.location}.` : "Saved.";
      load();
    }

    async function run(dest) {
      busy.value = true;
      error.value = notice.value = "";
      try {
        const job = await api(`/api/backups/destinations/${dest.id}/run`, { method: "POST" });
        jobStarted(job);
      } catch (err) {
        error.value = err.message;
      } finally {
        busy.value = false;
      }
    }

    function jobStarted(job) {
      notice.value = job.kind === "restore" ? "Restore started. Follow it below." : "Backup started. Follow it below.";
      openJob.value = job;
      loadJobs();
    }

    async function showPassword(dest) {
      try {
        passwordShown.value = { id: dest.id, ...(await api(`/api/backups/destinations/${dest.id}/password`, { method: "POST" })) };
      } catch (err) {
        error.value = err.message;
      }
    }

    async function remove(dest) {
      busy.value = true;
      try {
        await api(`/api/backups/destinations/${dest.id}`, { method: "DELETE" });
        confirmDelete.value = null;
        notice.value = `${dest.name} removed. The backups stored there were not deleted.`;
        await load();
      } catch (err) {
        error.value = err.message;
      } finally {
        busy.value = false;
      }
    }

    async function toggleJob(job) {
      if (openJob.value?.id === job.id) {
        openJob.value = null;
        return;
      }
      openJob.value = await api(`/api/backups/jobs/${job.id}`).catch((err) => {
        error.value = err.message;
        return null;
      });
    }

    function jobTarget(job) {
      if (job.kind === "restore") return `Restore ${job.domain}`;
      const dest = status.value?.destinations.find((d) => d.id === job.destination);
      const where = dest ? dest.name : "removed destination";
      return job.domains ? `Back up ${job.domains.join(", ")} to ${where}` : `Back up to ${where}`;
    }

    onMounted(load);
    onUnmounted(() => clearTimeout(timer));

    return {
      status, jobs, sites, error, notice, isAdmin, formFor, created, passwordShown, confirmDelete,
      openJob, busy, copied, formDone, run, jobStarted, showPassword, remove, toggleJob, jobTarget,
      scheduleText, keepText, formatDateTime, formatBytes, duration, TYPE_LABEL, STATUS_LABEL,
      STATUS_STATE, copyPassword: (text) => copy(text, copied),
    };
  },
  template: `
    <header class="page-head">
      <h1>Backups</h1>
      <div class="actions" v-if="isAdmin && status && !formFor">
        <button class="button" type="button" @click="formFor = 'new'; created = null">Add destination</button>
      </div>
    </header>

    <p v-if="error" class="alert" role="alert">{{ error }}</p>
    <p v-if="notice" class="notice" role="status">{{ notice }}</p>
    <p v-if="!status && !error" class="loading">Loading backups…</p>

    <template v-if="status">
      <p v-if="!status.restic_installed" class="alert">restic isn't installed. Run the Tela installer again.</p>

      <section v-if="created" class="panel credentials" role="status">
        <h2>Save this password</h2>
        <p class="muted">Backups in {{ created.name }} are encrypted with it. If this server is lost, you need it to get anything back. Store it somewhere else, like a password manager.</p>
        <dl class="credential-list">
          <div><dt>Password</dt><dd><code class="secret">{{ created.repo_password }}</code>
            <button class="button button-quiet" type="button" @click="copyPassword(created.repo_password)">{{ copied ? "Copied" : "Copy" }}</button></dd></div>
        </dl>
        <div class="actions"><button class="button button-secondary" type="button" @click="created = null">I've saved it</button></div>
      </section>

      <DestinationForm v-if="formFor" :key="formFor === 'new' ? 'new' : formFor.id"
                       :editing="formFor === 'new' ? null : formFor" @done="formDone" @cancel="formFor = null" />

      <template v-if="isAdmin">
        <section v-if="!status.destinations.length && !formFor" class="panel empty">
          <h2>No backups yet</h2>
          <p>Add a place to store backups: S3-compatible storage such as Backblaze B2, Wasabi or Amazon S3, a server over SFTP, or a folder on another disk. Tela then backs up every website's files, databases and email on a schedule, encrypted.</p>
          <button class="button" type="button" @click="formFor = 'new'">Add a destination</button>
        </section>

        <div v-else class="destinations">
          <section v-for="d in status.destinations" :key="d.id" class="panel destination">
            <div class="destination-head">
              <div>
                <h2>{{ d.name }}</h2>
                <p class="muted"><span class="badge">{{ TYPE_LABEL[d.type] }}</span> <code class="location">{{ d.location }}</code></p>
              </div>
              <button class="button button-secondary button-small" type="button" :disabled="busy || status.running.length > 0" @click="run(d)">Back up now</button>
            </div>
            <dl class="facts destination-facts">
              <div><dt>Schedule</dt><dd>{{ scheduleText(d.schedule) }}<span v-if="d.next_run" class="muted block">Next: {{ formatDateTime(d.next_run) }}</span></dd></div>
              <div><dt>Retention</dt><dd>{{ keepText(d.keep) }}</dd></div>
              <div><dt>Last backup</dt>
                <dd v-if="d.last && d.last.status"><span class="state" :data-state="STATUS_STATE[d.last.status]">{{ STATUS_LABEL[d.last.status] }}</span>
                  <span class="muted block">{{ formatDateTime(d.last.finished_at) }}<template v-if="d.last.message"> · {{ d.last.message }}</template></span></dd>
                <dd v-else class="muted">Not yet</dd>
              </div>
            </dl>
            <div v-if="passwordShown && passwordShown.id === d.id" class="key-box">
              <p>Repository password for <code>{{ passwordShown.location }}</code>:</p>
              <div class="key-row"><code class="secret">{{ passwordShown.repo_password }}</code>
                <button class="button button-quiet" type="button" @click="copyPassword(passwordShown.repo_password)">{{ copied ? "Copied" : "Copy" }}</button>
                <button class="button button-quiet" type="button" @click="passwordShown = null">Hide</button></div>
            </div>
            <div class="actions destination-actions">
              <button class="button button-quiet" type="button" @click="formFor = d">Edit</button>
              <button class="button button-quiet" type="button" @click="showPassword(d)">Show password</button>
              <template v-if="confirmDelete === d.id">
                <span class="confirm-text">Stop using it? The backups stay where they are.</span>
                <button class="button button-danger button-small" type="button" :disabled="busy" @click="remove(d)">Remove</button>
                <button class="button button-quiet" type="button" @click="confirmDelete = null">Cancel</button>
              </template>
              <button v-else class="button button-quiet danger-text" type="button" @click="confirmDelete = d.id">Remove</button>
            </div>
          </section>
        </div>
      </template>
      <section v-else-if="!status.destinations.length" class="panel empty">
        <h2>No backups set up</h2>
        <p>Your administrator hasn't set up backups yet.</p>
      </section>

      <RestorePanel v-if="status.destinations.length" :destinations="status.destinations" :sites="sites" @started="jobStarted" />

      <section v-if="jobs.length" class="panel table-panel jobs-panel">
        <h2 class="table-title">Recent activity</h2>
        <table class="table db-table">
          <thead><tr><th scope="col">What</th><th scope="col">Started</th><th scope="col">Result</th></tr></thead>
          <tbody>
            <template v-for="j in jobs" :key="j.id">
              <tr @click="toggleJob(j)" :aria-expanded="openJob && openJob.id === j.id ? 'true' : 'false'">
                <td><a href="#" @click.prevent.stop="toggleJob(j)">{{ jobTarget(j) }}</a></td>
                <td>{{ formatDateTime(j.started_at || j.created_at) }} <span class="muted">{{ duration(j) }}</span></td>
                <td><span class="state" :data-state="STATUS_STATE[j.status]">{{ STATUS_LABEL[j.status] }}</span>
                  <span v-if="j.message" class="muted"> · {{ j.message }}</span></td>
              </tr>
              <tr v-if="openJob && openJob.id === j.id" class="static-row job-detail">
                <td colspan="3">
                  <ul v-if="openJob.results.length" class="job-results">
                    <li v-for="r in openJob.results" :key="r.name">
                      <span class="state" :data-state="r.ok ? 'active' : 'failed'">{{ r.name }}</span>
                      <span class="muted" v-if="r.ok && r.size"> {{ formatBytes(r.size) }}</span>
                      <span class="muted" v-if="r.message"> {{ r.message }}</span>
                    </li>
                  </ul>
                  <pre class="job-log" v-if="openJob.log">{{ openJob.log }}</pre>
                </td>
              </tr>
            </template>
          </tbody>
        </table>
      </section>
    </template>
  `,
};
