import { ref, reactive, computed, onMounted } from "../vendor/vue.esm-browser.prod.js";
import { api, session, navigate, formatDate } from "./core.js";

function sslState(site) {
  if (!site.ssl_cert) return { state: "none", label: "Not secured" };
  const days = Math.floor((site.ssl_expires_at * 1000 - Date.now()) / 86400000);
  if (days < 0) return { state: "expired", label: "Certificate expired" };
  if (days <= 14) return { state: "expiring", label: `Expires in ${days} ${days === 1 ? "day" : "days"}` };
  return { state: "valid", label: `Secured until ${formatDate(site.ssl_expires_at)}` };
}

const phpVersions = ref([]);
async function loadPhpVersions() {
  if (!phpVersions.value.length) {
    phpVersions.value = (await api("/api/php/versions")).versions;
  }
  return phpVersions.value;
}

// ---------------------------------------------------------------------------
// List and create
// ---------------------------------------------------------------------------

export const WebsitesView = {
  setup() {
    const sites = ref(null);
    const error = ref("");
    const adding = ref(false);
    const form = reactive({ domain: "", php_version: "", include_www: true });
    const formError = ref("");
    const busy = ref(false);
    const isAdmin = computed(() => session.user?.role === "admin");

    async function load() {
      try {
        sites.value = (await api("/api/websites")).websites;
        error.value = "";
      } catch (err) {
        error.value = err.message;
      }
    }

    async function openForm() {
      adding.value = true;
      formError.value = "";
      try {
        const versions = await loadPhpVersions();
        if (!form.php_version) form.php_version = versions[versions.length - 1] || "";
      } catch (err) {
        formError.value = err.message;
      }
    }

    async function create() {
      formError.value = "";
      busy.value = true;
      try {
        const site = await api("/api/websites", { method: "POST", body: { ...form } });
        form.domain = "";
        adding.value = false;
        navigate(`/websites/${site.domain}`);
      } catch (err) {
        formError.value = err.message;
      } finally {
        busy.value = false;
      }
    }

    onMounted(load);
    return { sites, error, adding, form, formError, busy, isAdmin, phpVersions, openForm, create, navigate, sslState };
  },
  template: `
    <header class="page-head">
      <h1>Websites</h1>
      <button v-if="isAdmin && !adding && sites && sites.length" class="button" type="button" @click="openForm">Add website</button>
    </header>

    <form v-if="adding" class="panel form-panel" @submit.prevent="create">
      <h2>Add a website</h2>
      <p v-if="formError" class="alert" role="alert">{{ formError }}</p>
      <div class="form-grid">
        <div class="field">
          <label for="site-domain">Domain</label>
          <input id="site-domain" v-model.trim="form.domain" placeholder="example.com"
                 autocapitalize="none" spellcheck="false" required autofocus>
        </div>
        <div class="field">
          <label for="site-php">PHP version</label>
          <select id="site-php" v-model="form.php_version" required>
            <option v-for="v in phpVersions" :key="v" :value="v">PHP {{ v }}</option>
          </select>
        </div>
      </div>
      <label class="check">
        <input type="checkbox" v-model="form.include_www">
        <span>Also answer on www.{{ form.domain || "example.com" }}</span>
      </label>
      <div class="actions">
        <button class="button" type="submit" :disabled="busy || !form.domain || !form.php_version">
          {{ busy ? "Adding website…" : "Add website" }}
        </button>
        <button class="button button-quiet" type="button" @click="adding = false" :disabled="busy">Cancel</button>
      </div>
    </form>

    <p v-if="error" class="alert" role="alert">{{ error }}</p>
    <p v-else-if="!sites" class="loading">Loading websites…</p>

    <section v-else-if="!sites.length && !adding" class="panel empty">
      <h2>No websites yet</h2>
      <p>Each website gets its own folder, PHP version and a free SSL certificate.</p>
      <button v-if="isAdmin" class="button" type="button" @click="openForm">Add your first website</button>
    </section>

    <section v-else-if="sites.length" class="panel table-panel">
      <table class="table">
        <thead>
          <tr><th scope="col">Domain</th><th scope="col">PHP</th><th scope="col">SSL</th><th scope="col">Owner</th></tr>
        </thead>
        <tbody>
          <tr v-for="site in sites" :key="site.domain" @click="navigate('/websites/' + site.domain)">
            <td>
              <a :href="'/websites/' + site.domain" @click.prevent.stop="navigate('/websites/' + site.domain)">{{ site.domain }}</a>
              <small v-if="site.aliases.length">{{ site.aliases.join(", ") }}</small>
            </td>
            <td>{{ site.php_version }}</td>
            <td><span class="state" :data-state="sslState(site).state">{{ sslState(site).label }}</span></td>
            <td>{{ site.owner ? site.owner.username : "—" }}</td>
          </tr>
        </tbody>
      </table>
    </section>
  `,
};

// ---------------------------------------------------------------------------
// Detail
// ---------------------------------------------------------------------------

function useAction() {
  const busy = ref(false);
  const error = ref("");
  const done = ref("");
  async function run(fn, message) {
    busy.value = true;
    error.value = "";
    done.value = "";
    try {
      await fn();
      done.value = message;
    } catch (err) {
      error.value = err.message;
    } finally {
      busy.value = false;
    }
  }
  return reactive({ busy, error, done, run });
}

export const WebsiteDetailView = {
  props: ["domain"],
  setup(props) {
    const site = ref(null);
    const loadError = ref("");
    const isAdmin = computed(() => session.user?.role === "admin");

    const php = ref("");
    const aliases = ref([]);
    const newAlias = ref("");
    const email = ref("");
    const confirm = ref("");
    const removeFiles = ref(true);

    const phpAction = useAction();
    const aliasAction = useAction();
    const sslAction = useAction();
    const httpsAction = useAction();
    const deleteAction = useAction();

    function apply(data) {
      site.value = data;
      php.value = data.php_version;
      aliases.value = [...data.aliases];
    }

    async function load() {
      try {
        apply(await api(`/api/websites/${props.domain}`));
        await loadPhpVersions();
      } catch (err) {
        loadError.value = err.message;
      }
    }

    const ssl = computed(() => (site.value ? sslState(site.value) : null));
    const siteUrl = computed(() =>
      site.value ? `${site.value.ssl_cert ? "https" : "http"}://${site.value.domain}` : "",
    );
    const aliasesChanged = computed(
      () => site.value && aliases.value.join() !== site.value.aliases.join(),
    );

    function patch(body) {
      return api(`/api/websites/${props.domain}`, { method: "PATCH", body }).then(apply);
    }
    const savePhp = () => phpAction.run(() => patch({ php_version: php.value }), `Now running PHP ${php.value}.`);
    const saveAliases = () => aliasAction.run(() => patch({ aliases: aliases.value }), "Domains saved.");
    function addAlias() {
      const name = newAlias.value.trim().toLowerCase();
      if (name && !aliases.value.includes(name)) aliases.value.push(name);
      newAlias.value = "";
    }
    const removeAlias = (name) => (aliases.value = aliases.value.filter((a) => a !== name));

    function issueSsl() {
      sslAction.skipped = null;
      return sslAction.run(async () => {
        const result = await api(`/api/websites/${props.domain}/ssl`, {
          method: "POST",
          body: { email: email.value || null },
        });
        apply(result);
        if (result.skipped_names?.length) {
          sslAction.skipped = result.skipped_names;
        }
      }, "Certificate issued. It renews automatically.");
    }
    function toggleHttps(event) {
      const enabled = event.target.checked;
      return httpsAction.run(
        () => patch({ force_https: enabled }),
        enabled ? "Visitors are now sent to HTTPS." : "HTTP is allowed again.",
      );
    }
    function remove() {
      return deleteAction.run(async () => {
        await api(`/api/websites/${props.domain}`, {
          method: "DELETE",
          body: { confirm: confirm.value, remove_files: removeFiles.value },
        });
        navigate("/websites");
      }, "");
    }

    onMounted(load);
    return {
      site, loadError, isAdmin, php, phpVersions, aliases, newAlias, email, confirm, removeFiles,
      phpAction, aliasAction, sslAction, httpsAction, deleteAction, ssl, siteUrl, aliasesChanged,
      savePhp, saveAliases, addAlias, removeAlias, issueSsl, toggleHttps, remove, navigate, formatDate,
    };
  },
  template: `
    <nav class="crumbs" aria-label="Breadcrumb">
      <a href="/websites" @click.prevent="navigate('/websites')">Websites</a>
    </nav>
    <p v-if="loadError" class="alert" role="alert">{{ loadError }}</p>
    <p v-else-if="!site" class="loading">Loading…</p>
    <template v-else>
      <header class="page-head">
        <h1>{{ site.domain }}</h1>
        <a class="button button-secondary" :href="siteUrl" target="_blank" rel="noopener noreferrer">Visit site</a>
      </header>

      <div class="detail">
        <section class="panel">
          <h2>Overview</h2>
          <dl class="facts">
            <div><dt>Files</dt><dd><code>{{ site.home }}/public_html</code></dd></div>
            <div><dt>System user</dt><dd><code>{{ site.user }}</code></dd></div>
            <div><dt>Logs</dt><dd><code>{{ site.home }}/logs</code></dd></div>
            <div><dt>Added</dt><dd>{{ formatDate(site.created_at) }}</dd></div>
          </dl>
        </section>

        <section class="panel">
          <h2>SSL certificate</h2>
          <p class="ssl-status"><span class="state" :data-state="ssl.state">{{ ssl.label }}</span></p>
          <p v-if="site.ssl_cert" class="muted">Covers {{ site.ssl_names.join(", ") }}.</p>
          <p v-else class="muted">Point the domain's DNS A record at this server first. Let's Encrypt checks it before issuing.</p>

          <p v-if="sslAction.error" class="alert" role="alert">{{ sslAction.error }}</p>
          <p v-if="sslAction.done" class="notice" role="status">{{ sslAction.done }}
            <template v-if="sslAction.skipped"> Not included because they point elsewhere: {{ sslAction.skipped.join(", ") }}.</template>
          </p>

          <form class="inline-form" @submit.prevent="issueSsl">
            <div class="field">
              <label for="ssl-email">Email for expiry notices <span class="muted">(optional)</span></label>
              <input id="ssl-email" v-model.trim="email" type="email" autocomplete="email">
            </div>
            <button class="button" type="submit" :disabled="sslAction.busy">
              {{ sslAction.busy ? "Requesting certificate…" : site.ssl_cert ? "Renew certificate" : "Issue free certificate" }}
            </button>
          </form>

          <label class="check switch-row">
            <input type="checkbox" :checked="site.force_https" :disabled="!site.ssl_cert || httpsAction.busy" @change="toggleHttps">
            <span>Redirect all visitors to HTTPS</span>
          </label>
          <p v-if="httpsAction.error" class="alert" role="alert">{{ httpsAction.error }}</p>
          <p v-if="httpsAction.done" class="notice" role="status">{{ httpsAction.done }}</p>
        </section>

        <section class="panel">
          <h2>PHP</h2>
          <form class="inline-form" @submit.prevent="savePhp">
            <div class="field">
              <label for="php-version">Version</label>
              <select id="php-version" v-model="php">
                <option v-for="v in phpVersions" :key="v" :value="v">PHP {{ v }}</option>
              </select>
            </div>
            <button class="button" type="submit" :disabled="phpAction.busy || php === site.php_version">
              {{ phpAction.busy ? "Switching…" : "Switch version" }}
            </button>
          </form>
          <p v-if="phpAction.error" class="alert" role="alert">{{ phpAction.error }}</p>
          <p v-if="phpAction.done" class="notice" role="status">{{ phpAction.done }}</p>
        </section>

        <section class="panel">
          <h2>Domains</h2>
          <p class="muted">The site also answers on these names. Issue the certificate again after adding one.</p>
          <ul class="alias-list">
            <li class="alias-primary"><span>{{ site.domain }}</span><span class="muted">Main domain</span></li>
            <li v-for="name in aliases" :key="name">
              <span>{{ name }}</span>
              <button class="button button-quiet" type="button" @click="removeAlias(name)" :aria-label="'Remove ' + name">Remove</button>
            </li>
          </ul>
          <form class="inline-form" @submit.prevent="addAlias">
            <div class="field">
              <label for="new-alias">Add a domain</label>
              <input id="new-alias" v-model="newAlias" placeholder="shop.example.com" autocapitalize="none" spellcheck="false">
            </div>
            <button class="button button-secondary" type="submit" :disabled="!newAlias.trim()">Add</button>
          </form>
          <div v-if="aliasesChanged" class="actions">
            <button class="button" type="button" @click="saveAliases" :disabled="aliasAction.busy">
              {{ aliasAction.busy ? "Saving…" : "Save domains" }}
            </button>
            <button class="button button-quiet" type="button" @click="aliases = [...site.aliases]">Undo</button>
          </div>
          <p v-if="aliasAction.error" class="alert" role="alert">{{ aliasAction.error }}</p>
          <p v-if="aliasAction.done" class="notice" role="status">{{ aliasAction.done }}</p>
        </section>

        <section v-if="isAdmin" class="panel panel-danger panel-wide">
          <h2>Delete website</h2>
          <p class="muted">The site stops working immediately and its certificate is removed from this server.</p>
          <label class="check">
            <input type="checkbox" v-model="removeFiles">
            <span>Also delete its databases, all files in {{ site.home }} and the system user</span>
          </label>
          <form class="inline-form" @submit.prevent="remove">
            <div class="field">
              <label for="confirm-delete">Type <strong>{{ site.domain }}</strong> to confirm</label>
              <input id="confirm-delete" v-model.trim="confirm" autocapitalize="none" spellcheck="false" autocomplete="off">
            </div>
            <button class="button button-danger" type="submit" :disabled="deleteAction.busy || confirm.toLowerCase() !== site.domain">
              {{ deleteAction.busy ? "Deleting…" : "Delete website" }}
            </button>
          </form>
          <p v-if="deleteAction.error" class="alert" role="alert">{{ deleteAction.error }}</p>
        </section>
      </div>
    </template>
  `,
};
