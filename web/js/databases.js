import { ref, reactive, computed, onMounted } from "../vendor/vue.esm-browser.prod.js";
import { api, formatBytes } from "./core.js";

// Open phpMyAdmin in a new tab. The tab is opened right away (inside the click),
// otherwise browsers block it as a pop-up, and pointed at the sign-on link once
// the panel has one.
async function openPhpMyAdmin(domain, name, onError) {
  const tab = window.open("about:blank", "_blank");
  try {
    const link = await api(`/api/databases/${domain}/phpmyadmin`, {
      method: "POST",
      body: { name: name || null },
    });
    const url = `https://${location.hostname}:${link.port}${link.path}`;
    if (tab) {
      tab.opener = null;
      tab.location.href = url;
    } else {
      location.href = url;
    }
  } catch (err) {
    if (tab) tab.close();
    onError(err.message);
  }
}

async function copy(text, flag) {
  try {
    await navigator.clipboard.writeText(text);
    flag.value = true;
    setTimeout(() => (flag.value = false), 1500);
  } catch {
    // Clipboard unavailable (e.g. plain HTTP); the text is selectable anyway.
  }
}

const CredentialsCard = {
  props: ["credentials", "title"],
  emits: ["close"],
  setup(props) {
    const copiedUser = ref(false);
    const copiedPassword = ref(false);
    return {
      copiedUser,
      copiedPassword,
      copyUser: () => copy(props.credentials.user, copiedUser),
      copyPassword: () => copy(props.credentials.password, copiedPassword),
    };
  },
  template: `
    <section class="panel credentials" role="status">
      <h2>{{ title }}</h2>
      <p class="muted">Copy the password now. Tela doesn't store it; you can set a new one later.</p>
      <dl class="credential-list">
        <div><dt>Database</dt><dd><code>{{ credentials.name }}</code></dd></div>
        <div>
          <dt>User</dt>
          <dd><code>{{ credentials.user }}</code>
            <button class="button button-quiet" type="button" @click="copyUser">{{ copiedUser ? "Copied" : "Copy" }}</button></dd>
        </div>
        <div>
          <dt>Password</dt>
          <dd><code class="secret">{{ credentials.password }}</code>
            <button class="button button-quiet" type="button" @click="copyPassword">{{ copiedPassword ? "Copied" : "Copy" }}</button></dd>
        </div>
        <div><dt>Host</dt><dd><code>localhost</code></dd></div>
      </dl>
      <div class="actions"><button class="button button-secondary" type="button" @click="$emit('close')">Done</button></div>
    </section>
  `,
};

export const DatabasesView = {
  components: { CredentialsCard },
  setup() {
    const rows = ref(null);
    const sites = ref([]);
    const error = ref("");
    const notice = ref("");
    const credentials = ref(null);
    const credentialsTitle = ref("");

    const adding = ref(false);
    const form = reactive({ domain: "", name: "", password: "" });
    const formError = ref("");
    const busy = ref(false);

    const confirming = ref(null); // name of the database awaiting delete confirmation
    const working = ref(null); // name of the database with an action in flight

    const prefix = computed(() => {
      const site = sites.value.find((s) => s.domain === form.domain);
      return site ? `${site.user.slice(0, 16)}_` : "";
    });

    async function load() {
      try {
        const [dbs, websites] = await Promise.all([api("/api/databases"), api("/api/websites")]);
        rows.value = dbs.databases;
        sites.value = websites.websites;
        if (!form.domain && sites.value.length) form.domain = sites.value[0].domain;
        error.value = "";
      } catch (err) {
        error.value = err.message;
      }
    }

    function openForm() {
      adding.value = true;
      formError.value = "";
      credentials.value = null;
    }

    async function create() {
      formError.value = "";
      busy.value = true;
      try {
        const result = await api("/api/databases", {
          method: "POST",
          body: { domain: form.domain, name: form.name, password: form.password || null },
        });
        credentials.value = result;
        credentialsTitle.value = "Database created";
        form.name = "";
        form.password = "";
        adding.value = false;
        await load();
      } catch (err) {
        formError.value = err.message;
      } finally {
        busy.value = false;
      }
    }

    async function newPassword(row) {
      working.value = row.name;
      notice.value = "";
      error.value = "";
      try {
        credentials.value = await api(`/api/databases/${row.domain}/${row.name}/password`, {
          method: "POST",
          body: {},
        });
        credentialsTitle.value = "New password set";
      } catch (err) {
        error.value = err.message;
      } finally {
        working.value = null;
      }
    }

    async function remove(row) {
      working.value = row.name;
      error.value = "";
      try {
        await api(`/api/databases/${row.domain}/${row.name}`, { method: "DELETE" });
        notice.value = `Deleted ${row.name}.`;
        confirming.value = null;
        if (credentials.value?.name === row.name) credentials.value = null;
        await load();
      } catch (err) {
        error.value = err.message;
      } finally {
        working.value = null;
      }
    }

    function phpMyAdmin(row) {
      error.value = "";
      openPhpMyAdmin(row.domain, row.name, (message) => (error.value = message));
    }

    onMounted(load);
    return {
      rows, sites, error, notice, credentials, credentialsTitle, adding, form, formError, busy,
      confirming, working, prefix, openForm, create, newPassword, remove, phpMyAdmin, formatBytes,
    };
  },
  template: `
    <header class="page-head">
      <h1>Databases</h1>
      <button v-if="!adding && rows && rows.length && sites.length" class="button" type="button" @click="openForm">Add database</button>
    </header>

    <CredentialsCard v-if="credentials" :credentials="credentials" :title="credentialsTitle" @close="credentials = null" />

    <form v-if="adding" class="panel form-panel" @submit.prevent="create">
      <h2>Add a database</h2>
      <p v-if="formError" class="alert" role="alert">{{ formError }}</p>
      <div class="form-grid form-grid-even">
        <div class="field">
          <label for="db-site">Website</label>
          <select id="db-site" v-model="form.domain" required>
            <option v-for="s in sites" :key="s.domain" :value="s.domain">{{ s.domain }}</option>
          </select>
        </div>
        <div class="field">
          <label for="db-name">Name</label>
          <div class="prefixed">
            <span class="prefix" aria-hidden="true">{{ prefix }}</span>
            <input id="db-name" v-model.trim="form.name" placeholder="wordpress" autocapitalize="none"
                   spellcheck="false" required autofocus :aria-describedby="'db-name-help'">
          </div>
        </div>
      </div>
      <p id="db-name-help" class="muted hint">Lowercase letters, digits and underscores. The database user gets the same name.</p>
      <div class="field">
        <label for="db-password">Password <span class="muted">(optional)</span></label>
        <input id="db-password" v-model="form.password" type="text" autocomplete="off" spellcheck="false"
               placeholder="Leave empty to generate a strong one">
      </div>
      <div class="actions">
        <button class="button" type="submit" :disabled="busy || !form.domain || !form.name">
          {{ busy ? "Adding database…" : "Add database" }}
        </button>
        <button class="button button-quiet" type="button" @click="adding = false" :disabled="busy">Cancel</button>
      </div>
    </form>

    <p v-if="error" class="alert" role="alert">{{ error }}</p>
    <p v-if="notice" class="notice" role="status">{{ notice }}</p>
    <p v-if="!rows && !error" class="loading">Loading databases…</p>

    <section v-else-if="rows && !rows.length && !adding" class="panel empty">
      <h2>No databases yet</h2>
      <template v-if="sites.length">
        <p>Create a database for a website, for example for WordPress. You'll get its user and password right away.</p>
        <button class="button" type="button" @click="openForm">Add your first database</button>
      </template>
      <p v-else>Databases belong to a website. Add a website first.</p>
    </section>

    <section v-else-if="rows && rows.length" class="panel table-panel">
      <table class="table db-table">
        <thead>
          <tr><th scope="col">Database</th><th scope="col">Size</th><th scope="col"><span class="visually-hidden">Actions</span></th></tr>
        </thead>
        <tbody>
          <tr v-for="row in rows" :key="row.name" class="static-row">
            <td><code class="db-name">{{ row.name }}</code><small>{{ row.domain }}</small></td>
            <td>{{ formatBytes(row.size) }}</td>
            <td class="row-actions">
              <template v-if="confirming === row.name">
                <span class="confirm-text">Delete {{ row.name }} and all its data?</span>
                <button class="button button-danger button-small" type="button" :disabled="working === row.name" @click="remove(row)">
                  {{ working === row.name ? "Deleting…" : "Delete" }}
                </button>
                <button class="button button-quiet" type="button" @click="confirming = null">Cancel</button>
              </template>
              <template v-else>
                <button class="button button-secondary button-small" type="button" @click="phpMyAdmin(row)">phpMyAdmin</button>
                <button class="button button-quiet" type="button" :disabled="working === row.name" @click="newPassword(row)">New password</button>
                <button class="button button-quiet danger-text" type="button" @click="confirming = row.name">Delete</button>
              </template>
            </td>
          </tr>
        </tbody>
      </table>
    </section>
  `,
};
