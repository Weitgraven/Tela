import { ref, reactive, computed, onMounted, onUnmounted } from "../vendor/vue.esm-browser.prod.js";
import { api, session, navigate } from "./core.js";

const LANGUAGES = {
  en_US: "English (US)", en_GB: "English (UK)", nl_NL: "Nederlands", de_DE: "Deutsch",
  fr_FR: "Français", es_ES: "Español", it_IT: "Italiano", pt_PT: "Português", pt_BR: "Português (Brasil)",
  da_DK: "Dansk", sv_SE: "Svenska", nb_NO: "Norsk", fi: "Suomi", pl_PL: "Polski", cs_CZ: "Čeština",
};
const STATUS_LABEL = { ok: "Succeeded", partial: "Partly failed", failed: "Failed", running: "Running", queued: "Starting" };
const STATUS_STATE = { ok: "active", partial: "warning", failed: "failed", running: "busy", queued: "busy" };

function strongPassword() {
  const chars = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789-_";
  const bytes = crypto.getRandomValues(new Uint8Array(20));
  return Array.from(bytes, (b) => chars[b % chars.length]).join("");
}

function formatDateTime(ts) {
  if (!ts) return "";
  return new Date(ts * 1000).toLocaleString(undefined, { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

function jobText(job) {
  const target = job.target ? ` → ${job.target}` : "";
  return {
    "wp-install": `Install WordPress on ${job.domain}`,
    "wp-update": `Update ${job.domain}`,
    "wp-clone": job.mode === "staging" ? `Staging copy of ${job.domain}` : `Copy ${job.domain}${target}`,
    "wp-push": `Push ${job.domain}${target}`,
  }[job.kind] || job.kind;
}

// ---------------------------------------------------------------------------
// Install form
// ---------------------------------------------------------------------------

const InstallForm = {
  props: ["domain"],
  emits: ["started", "cancel"],
  setup(props, { emit }) {
    const form = reactive({
      title: "", admin_user: "", admin_email: session.user?.email || "", password: strongPassword(),
      locale: navigator.language?.toLowerCase().startsWith("nl") ? "nl_NL" : "en_US",
    });
    const busy = ref(false);
    const error = ref("");
    const copied = ref(false);

    async function submit() {
      busy.value = true;
      error.value = "";
      try {
        const job = await api(`/api/wordpress/sites/${props.domain}/install`, { method: "POST", body: form });
        emit("started", job, { user: form.admin_user, password: form.password });
      } catch (err) {
        error.value = err.message;
      } finally {
        busy.value = false;
      }
    }

    async function copyPassword() {
      try {
        await navigator.clipboard.writeText(form.password);
        copied.value = true;
        setTimeout(() => (copied.value = false), 1500);
      } catch {
        // selectable anyway
      }
    }

    return { form, busy, error, copied, submit, copyPassword, LANGUAGES, regenerate: () => (form.password = strongPassword()) };
  },
  template: `
    <form class="panel form-panel wp-form" @submit.prevent="submit">
      <h2>Install WordPress on {{ domain }}</h2>
      <p v-if="error" class="alert" role="alert">{{ error }}</p>
      <div class="form-grid form-grid-even">
        <div class="field span-2">
          <label for="wp-title">Site title</label>
          <input id="wp-title" v-model.trim="form.title" required autofocus>
        </div>
        <div class="field">
          <label for="wp-user">Admin user name</label>
          <input id="wp-user" v-model.trim="form.admin_user" autocomplete="off" autocapitalize="none" spellcheck="false" required>
        </div>
        <div class="field">
          <label for="wp-email">Admin email</label>
          <input id="wp-email" v-model.trim="form.admin_email" type="email" required>
        </div>
        <div class="field">
          <label for="wp-pass">Admin password</label>
          <div class="prefixed password-field">
            <input id="wp-pass" v-model="form.password" type="text" autocomplete="off" spellcheck="false" minlength="10" required>
            <button class="button button-quiet" type="button" @click="copyPassword">{{ copied ? "Copied" : "Copy" }}</button>
            <button class="button button-quiet" type="button" @click="regenerate">New</button>
          </div>
        </div>
        <div class="field">
          <label for="wp-lang">Language</label>
          <select id="wp-lang" v-model="form.locale">
            <option v-for="(label, code) in LANGUAGES" :key="code" :value="code">{{ label }}</option>
          </select>
        </div>
      </div>
      <p class="muted hint">Tela creates a database for it. Save the password: Tela doesn't keep it.</p>
      <div class="actions">
        <button class="button" type="submit" :disabled="busy || !form.title || !form.admin_user || !form.admin_email">{{ busy ? "Starting…" : "Install WordPress" }}</button>
        <button class="button button-quiet" type="button" @click="$emit('cancel')" :disabled="busy">Cancel</button>
      </div>
    </form>
  `,
};

// ---------------------------------------------------------------------------
// One WordPress site
// ---------------------------------------------------------------------------

const ManagePanel = {
  props: ["site", "sites"],
  emits: ["started", "close"],
  setup(props, { emit }) {
    const info = ref(null);
    const error = ref("");
    const busy = ref(false);
    const confirmPush = ref(false);
    const push = reactive({ files: true, database: true });
    const cloning = ref(false);
    const target = ref("");
    const isAdmin = computed(() => session.user?.role === "admin");

    async function load() {
      error.value = "";
      try {
        info.value = await api(`/api/wordpress/sites/${props.site.domain}`);
      } catch (err) {
        error.value = err.message;
      }
    }

    async function start(path, body = {}) {
      busy.value = true;
      error.value = "";
      try {
        const job = await api(`/api/wordpress/sites/${props.site.domain}/${path}`, { method: "POST", body });
        confirmPush.value = cloning.value = false;
        emit("started", job);
      } catch (err) {
        error.value = err.message;
      } finally {
        busy.value = false;
      }
    }

    const updates = computed(() => {
      if (!info.value) return 0;
      const count = (list) => list.filter((x) => x.update === "available").length;
      return (info.value.core_update ? 1 : 0) + count(info.value.plugins) + count(info.value.themes);
    });
    // Plugins, plus the themes that matter: the one in use and any with an update.
    const shownTheme = (t) => t.status !== "inactive" || t.update === "available";
    const rows = computed(() => !info.value ? [] : [
      ...info.value.plugins.map((x) => ({ ...x, kind: "Plugin" })),
      ...info.value.themes.filter(shownTheme).map((x) => ({ ...x, kind: "Theme" })),
    ]);
    const hiddenThemes = computed(() => (info.value ? info.value.themes.filter((t) => !shownTheme(t)).length : 0));
    const targets = computed(() => props.sites.filter((s) => !s.installed && s.domain !== props.site.domain).map((s) => s.domain));

    onMounted(load);
    return { info, error, busy, confirmPush, push, cloning, target, isAdmin, updates, targets, rows, hiddenThemes, start, load, navigate };
  },
  template: `
    <section class="panel wp-manage">
      <div class="destination-head">
        <div>
          <h2>{{ site.domain }}</h2>
          <p class="muted">
            <span class="badge" v-if="site.staging_of">Staging copy of {{ site.staging_of }}</span>
            <a :href="site.url" target="_blank" rel="noopener">Visit site</a>
            <a :href="site.url + '/wp-admin/'" target="_blank" rel="noopener">Dashboard (wp-admin)</a>
          </p>
        </div>
        <button class="button button-quiet" type="button" @click="$emit('close')">Close</button>
      </div>
      <p v-if="error" class="alert" role="alert">{{ error }}</p>
      <p v-if="!info && !error" class="loading">Checking for updates…</p>

      <template v-if="info">
        <p v-for="e in info.errors" :key="e" class="muted hint">{{ e }}</p>
        <dl class="facts destination-facts">
          <div><dt>WordPress</dt><dd>{{ info.version }}
            <span v-if="info.core_update" class="state block" data-state="warning">{{ info.core_update }} available</span>
            <span v-else class="state block" data-state="active">Up to date</span></dd></div>
          <div><dt>Plugins</dt><dd>{{ info.plugins.length }}
            <span class="muted block">{{ info.plugins.filter(p => p.update === 'available').length }} to update</span></dd></div>
          <div><dt>Search engines</dt><dd>{{ info.search_engines ? "Allowed" : "Asked not to index" }}</dd></div>
        </dl>

        <table v-if="rows.length" class="table db-table wp-table">
          <thead><tr><th scope="col">Plugin or theme</th><th scope="col">Version</th><th scope="col">Status</th></tr></thead>
          <tbody>
            <tr v-for="p in rows" :key="p.kind + p.name" class="static-row">
              <td><span class="db-name">{{ p.title || p.name }}</span> <span class="muted">{{ p.kind }}</span></td>
              <td>{{ p.version }}<span v-if="p.update === 'available'" class="state" data-state="warning"> → {{ p.update_version }}</span></td>
              <td class="muted">{{ p.status }}</td>
            </tr>
          </tbody>
        </table>
        <p v-if="hiddenThemes" class="muted hint">{{ hiddenThemes }} more installed {{ hiddenThemes === 1 ? "theme" : "themes" }}, not in use and up to date.</p>

        <div class="actions">
          <button class="button" type="button" :disabled="busy || !updates" @click="start('update', { core: true, plugins: true, themes: true })">
            {{ updates ? "Update everything (" + updates + ")" : "Everything is up to date" }}</button>
          <button v-if="!site.staging && !site.staging_of" class="button button-secondary" type="button" :disabled="busy" @click="start('staging')">Make a staging copy</button>
          <a v-if="site.staging" class="button button-quiet" :href="'#'" @click.prevent="$emit('close', site.staging)">Staging: {{ site.staging }}</a>
          <button v-if="!site.staging_of" class="button button-quiet" type="button" @click="cloning = !cloning">Copy to another website</button>
        </div>
        <p class="muted hint" v-if="!site.staging && !site.staging_of">A staging copy runs on staging.{{ site.domain }}. Give that name a DNS record (Tela adds it if the domain's DNS is in Tela).</p>

        <form v-if="cloning" class="inline-form wp-clone" @submit.prevent="start('clone', { target })">
          <div class="field">
            <label for="clone-target">Copy {{ site.domain }} to</label>
            <input id="clone-target" v-model.trim="target" list="clone-targets" placeholder="new-domain.com" autocapitalize="none" spellcheck="false" required>
            <datalist id="clone-targets"><option v-for="t in targets" :key="t" :value="t"></option></datalist>
          </div>
          <button class="button button-secondary" type="submit" :disabled="busy || !target">Copy</button>
          <p class="muted hint full">Copies files and database and changes the addresses in it. {{ isAdmin ? "A new website is created if it doesn't exist; an existing one must be empty." : "Choose one of your empty websites." }}</p>
        </form>

        <div v-if="site.staging_of" class="panel-danger push-box">
          <h3>Push to {{ site.staging_of }}</h3>
          <p class="muted">Replaces the live site with this staging copy. A copy of the live site as it is now is kept on the server (in <code>.tela/before-push</code>).</p>
          <div class="check-row">
            <label class="check"><input type="checkbox" v-model="push.files" @change="confirmPush = false"><span>Files</span></label>
            <label class="check"><input type="checkbox" v-model="push.database" @change="confirmPush = false"><span>Database (posts, pages, settings, orders…)</span></label>
          </div>
          <p v-if="push.database" class="muted hint">Anything added on the live site since the copy was made, like orders or comments, is lost.</p>
          <div class="actions">
            <button v-if="!confirmPush" class="button button-secondary" type="button" :disabled="!push.files && !push.database" @click="confirmPush = true">Push to live</button>
            <template v-else>
              <button class="button button-danger" type="button" :disabled="busy" @click="start('push', push)">Yes, replace {{ site.staging_of }}</button>
              <button class="button button-quiet" type="button" @click="confirmPush = false">Cancel</button>
            </template>
          </div>
        </div>
      </template>
    </section>
  `,
};

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------

export const WordPressView = {
  components: { InstallForm, ManagePanel },
  setup() {
    const sites = ref(null);
    const jobs = ref([]);
    const error = ref("");
    const notice = ref("");
    const installing = ref(null);
    const managing = ref(null);
    const openJob = ref(null);
    const credentials = ref(null);
    let timer = null;

    async function load() {
      try {
        const [s, j] = await Promise.all([api("/api/wordpress/sites"), api("/api/wordpress/jobs")]);
        sites.value = s.sites;
        jobs.value = j.jobs;
        if (openJob.value) openJob.value = await api(`/api/wordpress/jobs/${openJob.value.id}`);
        error.value = "";
      } catch (err) {
        error.value = err.message;
      }
      clearTimeout(timer);
      if (jobs.value.some((j) => j.status === "queued" || j.status === "running")) {
        timer = setTimeout(load, 3000);
      }
    }

    function started(job, creds) {
      installing.value = null;
      managing.value = null;
      openJob.value = job;
      if (creds) credentials.value = { domain: job.domain, ...creds };
      notice.value = "Started. Follow it under Recent activity.";
      load();
    }

    function manage(site) {
      installing.value = null;
      managing.value = site;
    }

    function closeManage(other) {
      managing.value = other ? sites.value.find((s) => s.domain === other) || null : null;
    }

    async function toggleJob(job) {
      if (openJob.value?.id === job.id) {
        openJob.value = null;
        return;
      }
      openJob.value = await api(`/api/wordpress/jobs/${job.id}`).catch(() => null);
    }

    onMounted(load);
    onUnmounted(() => clearTimeout(timer));
    return {
      sites, jobs, error, notice, installing, managing, openJob, credentials, started, manage,
      closeManage, toggleJob, jobText, formatDateTime, STATUS_LABEL, STATUS_STATE, navigate,
    };
  },
  template: `
    <header class="page-head"><h1>WordPress</h1></header>
    <p v-if="error" class="alert" role="alert">{{ error }}</p>
    <p v-if="notice" class="notice" role="status">{{ notice }}</p>
    <p v-if="!sites && !error" class="loading">Loading…</p>

    <section v-if="credentials" class="panel credentials" role="status">
      <h2>WordPress admin for {{ credentials.domain }}</h2>
      <p class="muted">Once the install has finished, sign in at <a :href="'//' + credentials.domain + '/wp-admin/'" target="_blank" rel="noopener">{{ credentials.domain }}/wp-admin</a>. Save the password now.</p>
      <dl class="credential-list">
        <div><dt>User</dt><dd><code>{{ credentials.user }}</code></dd></div>
        <div><dt>Password</dt><dd><code class="secret">{{ credentials.password }}</code></dd></div>
      </dl>
      <div class="actions"><button class="button button-secondary" type="button" @click="credentials = null">I've saved it</button></div>
    </section>

    <InstallForm v-if="installing" :key="installing" :domain="installing" @started="started" @cancel="installing = null" />
    <ManagePanel v-if="managing" :key="managing.domain" :site="managing" :sites="sites" @started="started" @close="closeManage" />

    <template v-if="sites">
      <section v-if="!sites.length" class="panel empty">
        <h2>No websites yet</h2>
        <p>Create a website first, then install WordPress on it here.</p>
        <button class="button" type="button" @click="navigate('/websites')">Go to websites</button>
      </section>
      <section v-else class="panel table-panel">
        <table class="table db-table">
          <thead><tr><th scope="col">Website</th><th scope="col">WordPress</th><th scope="col"><span class="visually-hidden">Actions</span></th></tr></thead>
          <tbody>
            <tr v-for="s in sites" :key="s.domain" class="static-row">
              <td><span class="db-name">{{ s.domain }}</span>
                <small v-if="s.staging" class="muted block">Staging: {{ s.staging }}</small>
                <small v-if="s.staging_of" class="muted block">Staging copy of {{ s.staging_of }}</small></td>
              <td><span v-if="s.installed">{{ s.version }}</span><span v-else class="muted">Not installed</span></td>
              <td class="row-actions">
                <button v-if="s.installed" class="button button-secondary button-small" type="button" @click="manage(s)">Manage</button>
                <button v-else class="button button-quiet" type="button" @click="installing = s.domain; managing = null">Install WordPress</button>
              </td>
            </tr>
          </tbody>
        </table>
      </section>

      <section v-if="jobs.length" class="panel table-panel jobs-panel">
        <h2 class="table-title">Recent activity</h2>
        <table class="table db-table">
          <thead><tr><th scope="col">What</th><th scope="col">Started</th><th scope="col">Result</th></tr></thead>
          <tbody>
            <template v-for="j in jobs" :key="j.id">
              <tr @click="toggleJob(j)">
                <td><a href="#" @click.prevent.stop="toggleJob(j)">{{ jobText(j) }}</a></td>
                <td>{{ formatDateTime(j.started_at || j.created_at) }}</td>
                <td><span class="state" :data-state="STATUS_STATE[j.status]">{{ STATUS_LABEL[j.status] }}</span>
                  <span v-if="j.message" class="muted"> · {{ j.message }}</span></td>
              </tr>
              <tr v-if="openJob && openJob.id === j.id" class="static-row job-detail">
                <td colspan="3">
                  <ul v-if="openJob.results.length" class="job-results">
                    <li v-for="(r, i) in openJob.results" :key="i">
                      <span class="state" :data-state="r.ok ? 'active' : 'failed'">{{ r.name }}</span>
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
