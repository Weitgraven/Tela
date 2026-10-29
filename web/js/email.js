import { ref, reactive, computed, onMounted } from "../vendor/vue.esm-browser.prod.js";
import { api, session, navigate, formatBytes, formatDate } from "./core.js";

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
// Mail server settings (administrators)
// ---------------------------------------------------------------------------

const MailServer = {
  props: ["status"],
  emits: ["saved"],
  setup(props, { emit }) {
    const hostname = ref(props.status.hostname);
    const email = ref("");
    const saving = ref(false);
    const issuing = ref(false);
    const error = ref("");
    const done = ref("");

    async function save() {
      saving.value = true;
      error.value = done.value = "";
      try {
        await api("/api/mail/settings", { method: "PUT", body: { hostname: hostname.value } });
        done.value = "Host name saved. Get a certificate for it once its A record points here.";
        emit("saved");
      } catch (err) {
        error.value = err.message;
      } finally {
        saving.value = false;
      }
    }

    async function issue() {
      issuing.value = true;
      error.value = done.value = "";
      try {
        await api("/api/mail/ssl", { method: "POST", body: { email: email.value || null } });
        done.value = "Certificate installed. Mail apps and webmail now use it.";
        emit("saved");
      } catch (err) {
        error.value = err.message;
      } finally {
        issuing.value = false;
      }
    }

    return { hostname, email, saving, issuing, error, done, save, issue, formatDate };
  },
  template: `
    <section class="panel mail-server">
      <h2>Mail server</h2>
      <p v-if="error" class="alert" role="alert">{{ error }}</p>
      <p v-if="done" class="notice" role="status">{{ done }}</p>
      <form class="inline-form" @submit.prevent="save">
        <div class="field">
          <label for="mail-host">Host name</label>
          <input id="mail-host" v-model.trim="hostname" placeholder="mail.example.com" autocapitalize="none" spellcheck="false" required>
        </div>
        <button class="button button-secondary" type="submit" :disabled="saving || !hostname || hostname === status.hostname">{{ saving ? "Saving…" : "Save" }}</button>
        <p class="muted hint full">Mail apps connect to this name, other mail servers look it up in your MX records, and webmail runs on it. Give it an A record pointing at this server.</p>
      </form>

      <div class="mail-cert">
        <h3>Certificate</h3>
        <p v-if="status.ssl_issued">
          <span class="state" data-state="active">Let's Encrypt</span>
          <span class="muted" v-if="status.ssl_expires_at"> renews automatically, valid until {{ formatDate(status.ssl_expires_at) }}</span>
        </p>
        <p v-else class="muted">Using the panel's self-signed certificate: mail apps will warn about it.</p>
        <form class="inline-form" @submit.prevent="issue">
          <div class="field">
            <label for="mail-cert-email">Email for expiry notices <span class="muted">(optional)</span></label>
            <input id="mail-cert-email" v-model.trim="email" type="email" autocomplete="email">
          </div>
          <button class="button button-secondary" type="submit" :disabled="issuing || !status.hostname">{{ issuing ? "Requesting…" : status.ssl_issued ? "Renew now" : "Get certificate" }}</button>
        </form>
      </div>
    </section>
  `,
};

// ---------------------------------------------------------------------------
// Mail domains
// ---------------------------------------------------------------------------

export const EmailView = {
  components: { MailServer },
  setup() {
    const status = ref(null);
    const domains = ref(null);
    const sites = ref([]);
    const error = ref("");
    const isAdmin = computed(() => session.user?.role === "admin");
    const showServer = ref(null);
    const adding = ref(false);
    const form = reactive({ domain: "", dns: true });
    const formError = ref("");
    const busy = ref(false);

    async function load() {
      try {
        const [st, d, w] = await Promise.all([
          api("/api/mail/status"),
          api("/api/mail/domains"),
          api("/api/websites"),
        ]);
        status.value = st;
        domains.value = d.domains;
        // Show the server settings until there's something else to look at.
        if (showServer.value === null) showServer.value = !st.ssl_issued || !d.domains.length;
        sites.value = w.websites.map((s) => s.domain);
        error.value = "";
      } catch (err) {
        error.value = err.message;
      }
    }

    const available = computed(() =>
      sites.value.filter((s) => !(domains.value || []).some((d) => d.domain === s)),
    );

    function openForm() {
      adding.value = true;
      formError.value = "";
      form.domain = available.value[0] || "";
    }

    async function create() {
      busy.value = true;
      formError.value = "";
      try {
        const result = await api("/api/mail/domains", {
          method: "POST",
          body: { domain: form.domain, dns: form.dns },
        });
        navigate(`/email/${result.domain}`);
      } catch (err) {
        formError.value = err.message;
      } finally {
        busy.value = false;
      }
    }

    onMounted(load);
    return {
      status, domains, sites, available, error, isAdmin, showServer, adding, form, formError,
      busy, openForm, create, load, navigate,
    };
  },
  template: `
    <header class="page-head">
      <h1>Email</h1>
      <div class="actions" v-if="status && status.installed">
        <a v-if="status.webmail_installed && status.webmail_url" class="button button-quiet" :href="status.webmail_url" target="_blank" rel="noopener">Webmail</a>
        <button v-if="isAdmin" class="button button-quiet" type="button" @click="showServer = !showServer">{{ showServer ? "Hide mail server" : "Mail server" }}</button>
        <button v-if="!adding && domains && (isAdmin || available.length)" class="button" type="button" @click="openForm">Add domain</button>
      </div>
    </header>

    <p v-if="error" class="alert" role="alert">{{ error }}</p>
    <p v-else-if="!status" class="loading">Loading email…</p>

    <section v-else-if="!status.installed" class="panel empty">
      <h2>The mail server isn't installed</h2>
      <p>Run the Tela installer again to add it.</p>
    </section>

    <template v-else>
      <MailServer v-if="isAdmin && showServer" :status="status" @saved="load" />

      <form v-if="adding" class="panel form-panel" @submit.prevent="create">
        <h2>Add a mail domain</h2>
        <p v-if="formError" class="alert" role="alert">{{ formError }}</p>
        <div class="field">
          <label for="mail-domain">Domain</label>
          <input v-if="isAdmin" id="mail-domain" v-model.trim="form.domain" list="mail-domain-sites" placeholder="example.com" autocapitalize="none" spellcheck="false" required>
          <select v-else id="mail-domain" v-model="form.domain" required>
            <option v-for="s in available" :key="s" :value="s">{{ s }}</option>
          </select>
          <datalist id="mail-domain-sites"><option v-for="s in available" :key="s" :value="s"></option></datalist>
        </div>
        <label class="check">
          <input type="checkbox" v-model="form.dns">
          <span>Add the MX, SPF, DKIM and DMARC records if the domain has a DNS zone in Tela</span>
        </label>
        <div class="actions">
          <button class="button" type="submit" :disabled="busy || !form.domain">{{ busy ? "Adding…" : "Add domain" }}</button>
          <button class="button button-quiet" type="button" @click="adding = false" :disabled="busy">Cancel</button>
        </div>
      </form>

      <section v-if="domains && !domains.length && !adding" class="panel empty">
        <h2>No mail domains yet</h2>
        <p>Mailboxes belong to a domain. Add the domain first (for example cvgalerij.nl), then create mailboxes like info@ and forwarders on its page.</p>
        <button v-if="isAdmin || available.length" class="button" type="button" @click="openForm">Add your first domain</button>
        <p v-else class="muted">Email can be added for the domains of your websites.</p>
      </section>

      <section v-else-if="domains && domains.length" class="panel table-panel">
        <table class="table">
          <thead><tr><th scope="col">Domain</th><th scope="col">Mailboxes</th><th scope="col">Forwarders</th></tr></thead>
          <tbody>
            <tr v-for="d in domains" :key="d.domain" @click="navigate('/email/' + d.domain)">
              <td><a :href="'/email/' + d.domain" @click.prevent.stop="navigate('/email/' + d.domain)">{{ d.domain }}</a></td>
              <td>{{ d.mailboxes }}</td>
              <td>{{ d.aliases }}</td>
            </tr>
          </tbody>
        </table>
      </section>
    </template>
  `,
};

// ---------------------------------------------------------------------------
// One mail domain
// ---------------------------------------------------------------------------

const PasswordCard = {
  props: ["address", "password", "hostname", "title"],
  emits: ["close"],
  setup(props) {
    const copiedAddress = ref(false);
    const copiedPassword = ref(false);
    return {
      copiedAddress,
      copiedPassword,
      copyAddress: () => copy(props.address, copiedAddress),
      copyPassword: () => copy(props.password, copiedPassword),
    };
  },
  template: `
    <section class="panel credentials" role="status">
      <h2>{{ title }}</h2>
      <p class="muted">Copy the password now. Tela doesn't store it; you can set a new one later.</p>
      <dl class="credential-list">
        <div><dt>Address</dt><dd><code>{{ address }}</code>
          <button class="button button-quiet" type="button" @click="copyAddress">{{ copiedAddress ? "Copied" : "Copy" }}</button></dd></div>
        <div><dt>Password</dt><dd><code class="secret">{{ password }}</code>
          <button class="button button-quiet" type="button" @click="copyPassword">{{ copiedPassword ? "Copied" : "Copy" }}</button></dd></div>
        <div><dt>Incoming</dt><dd><code>IMAP {{ hostname }}, port 993, SSL/TLS</code></dd></div>
        <div><dt>Outgoing</dt><dd><code>SMTP {{ hostname }}, port 465 SSL/TLS or 587 STARTTLS</code></dd></div>
      </dl>
      <p class="muted">The user name is the full address.</p>
      <div class="actions"><button class="button button-secondary" type="button" @click="$emit('close')">Done</button></div>
    </section>
  `,
};

function emptyMailbox() {
  return { local: "", name: "", password: "", quota_mb: 1024 };
}

export const EmailDomainView = {
  props: ["domain"],
  components: { PasswordCard },
  setup(props) {
    const data = ref(null);
    const status = ref(null);
    const error = ref("");
    const notice = ref("");
    const busy = ref(false);
    const card = ref(null); // { title, address, password }

    const addingBox = ref(false);
    const box = reactive(emptyMailbox());
    const boxError = ref("");
    const editingQuota = ref(null);
    const quotaValue = ref(0);
    const confirmBox = ref(null);

    const addingAlias = ref(false);
    const alias = reactive({ local: "", destinations: "" });
    const aliasError = ref("");
    const confirmAlias = ref(null);

    const dnsResult = ref(null);
    const confirmDomain = ref(false);
    const base = computed(() => `/api/mail/domains/${props.domain}`);

    async function load() {
      try {
        const [d, s] = await Promise.all([api(base.value), api("/api/mail/status")]);
        data.value = d;
        status.value = s;
        error.value = "";
      } catch (err) {
        error.value = err.message;
      }
    }

    async function run(fn, onError = (m) => (error.value = m)) {
      busy.value = true;
      error.value = "";
      notice.value = "";
      try {
        await fn();
      } catch (err) {
        onError(err.message);
      } finally {
        busy.value = false;
      }
    }

    function addMailbox() {
      return run(
        async () => {
          const result = await api(`${base.value}/mailboxes`, {
            method: "POST",
            body: {
              local: box.local,
              name: box.name || null,
              password: box.password || null,
              quota_mb: Number(box.quota_mb) || 0,
            },
          });
          addingBox.value = false;
          if (result.password) {
            card.value = { title: "Mailbox created", address: result.address, password: result.password };
          } else {
            notice.value = `${result.address} created.`;
          }
          Object.assign(box, emptyMailbox());
          await load();
        },
        (m) => (boxError.value = m),
      );
    }

    function newPassword(m) {
      return run(async () => {
        const result = await api(`${base.value}/mailboxes/${m.local}`, {
          method: "PUT",
          body: { password: null },
        });
        card.value = { title: "New password set", address: result.address, password: result.password };
      });
    }

    function setEnabled(m, enabled) {
      return run(async () => {
        await api(`${base.value}/mailboxes/${m.local}`, { method: "PUT", body: { enabled } });
        notice.value = enabled
          ? `${m.address} can sign in again.`
          : `${m.address} can no longer sign in. It still receives mail.`;
        await load();
      });
    }

    function startQuota(m) {
      editingQuota.value = m.local;
      quotaValue.value = m.quota_mb;
    }

    function saveQuota(m) {
      return run(async () => {
        await api(`${base.value}/mailboxes/${m.local}`, {
          method: "PUT",
          body: { quota_mb: Number(quotaValue.value) || 0 },
        });
        editingQuota.value = null;
        await load();
      });
    }

    function deleteMailbox(m) {
      return run(async () => {
        await api(`${base.value}/mailboxes/${m.local}`, { method: "DELETE" });
        confirmBox.value = null;
        notice.value = `${m.address} and its mail were deleted.`;
        await load();
      });
    }

    function addAlias() {
      return run(
        async () => {
          await api(`${base.value}/aliases`, {
            method: "POST",
            body: {
              local: alias.local,
              destinations: alias.destinations.split(/[\s,;]+/).filter(Boolean),
            },
          });
          addingAlias.value = false;
          alias.local = alias.destinations = "";
          await load();
        },
        (m) => (aliasError.value = m),
      );
    }

    function deleteAlias(a) {
      return run(async () => {
        await api(`${base.value}/aliases/${encodeURIComponent(a.local)}`, { method: "DELETE" });
        confirmAlias.value = null;
        await load();
      });
    }

    function addDns() {
      return run(async () => {
        dnsResult.value = await api(`${base.value}/dns`, { method: "POST" });
      });
    }

    function deleteDomain() {
      return run(async () => {
        await api(base.value, { method: "DELETE" });
        navigate("/email");
      });
    }

    function usage(m) {
      if (m.usage === null || m.usage === undefined) return null;
      const used = formatBytes(m.usage);
      if (!m.quota_mb) return { label: `${used} used`, pct: null };
      const pct = Math.min(100, (m.usage / (m.quota_mb * 1024 * 1024)) * 100);
      return { label: `${used} of ${formatBytes(m.quota_mb * 1024 * 1024)}`, pct };
    }

    function aliasAddress(a) {
      return a.local === "*" ? `Anything else @${props.domain}` : `${a.local}@${props.domain}`;
    }

    function recordName(r) {
      return r.name === "@" ? props.domain : `${r.name}.${props.domain}`;
    }

    const hasCatchAll = computed(() => data.value?.aliases.some((a) => a.local === "*"));
    const webmail = computed(() =>
      status.value?.webmail_installed && status.value.webmail_url ? status.value.webmail_url : "",
    );

    onMounted(load);
    return {
      data, status, error, notice, busy, card, addingBox, box, boxError, editingQuota, quotaValue,
      confirmBox, addingAlias, alias, aliasError, confirmAlias, dnsResult, confirmDomain,
      hasCatchAll, webmail, addMailbox, newPassword, setEnabled, startQuota, saveQuota,
      deleteMailbox, addAlias, deleteAlias, addDns, deleteDomain, usage, aliasAddress, recordName,
      navigate,
    };
  },
  template: `
    <nav class="crumbs" aria-label="Breadcrumb"><a href="/email" @click.prevent="navigate('/email')">Email</a></nav>
    <p v-if="error && !data" class="alert" role="alert">{{ error }}</p>
    <p v-else-if="!data" class="loading">Loading…</p>
    <template v-if="data">
      <header class="page-head">
        <h1>{{ domain }}</h1>
        <div class="actions">
          <a v-if="webmail" class="button button-quiet" :href="webmail" target="_blank" rel="noopener">Webmail</a>
          <button v-if="!addingBox" class="button" type="button" @click="addingBox = true; boxError = ''">Add mailbox</button>
        </div>
      </header>
      <p v-if="error" class="alert" role="alert">{{ error }}</p>
      <p v-if="notice" class="notice" role="status">{{ notice }}</p>

      <PasswordCard v-if="card" :title="card.title" :address="card.address" :password="card.password"
                    :hostname="data.hostname" @close="card = null" />

      <form v-if="addingBox" class="panel form-panel" @submit.prevent="addMailbox">
        <h2>Add a mailbox</h2>
        <p v-if="boxError" class="alert" role="alert">{{ boxError }}</p>
        <div class="form-grid form-grid-even">
          <div class="field">
            <label for="box-local">Address</label>
            <div class="prefixed suffixed">
              <input id="box-local" v-model.trim="box.local" placeholder="info" autocapitalize="none" spellcheck="false" required autofocus>
              <span class="prefix" aria-hidden="true">@{{ domain }}</span>
            </div>
          </div>
          <div class="field">
            <label for="box-name">Name <span class="muted">(optional)</span></label>
            <input id="box-name" v-model.trim="box.name" placeholder="Sales team">
          </div>
          <div class="field">
            <label for="box-password">Password <span class="muted">(optional)</span></label>
            <input id="box-password" v-model="box.password" type="text" autocomplete="off" spellcheck="false" placeholder="Leave empty to generate one">
          </div>
          <div class="field">
            <label for="box-quota">Size limit in MB <span class="muted">(0 = no limit)</span></label>
            <input id="box-quota" v-model.number="box.quota_mb" type="number" min="0" step="256">
          </div>
        </div>
        <div class="actions">
          <button class="button" type="submit" :disabled="busy || !box.local">{{ busy ? "Creating…" : "Create mailbox" }}</button>
          <button class="button button-quiet" type="button" @click="addingBox = false" :disabled="busy">Cancel</button>
        </div>
      </form>

      <section class="panel table-panel">
        <h2 class="table-title">Mailboxes</h2>
        <p v-if="!data.mailboxes.length" class="muted table-empty">No mailboxes yet.</p>
        <table v-else class="table db-table mail-table">
          <thead><tr><th scope="col">Address</th><th scope="col">Storage</th><th scope="col"><span class="visually-hidden">Actions</span></th></tr></thead>
          <tbody>
            <tr v-for="m in data.mailboxes" :key="m.local" class="static-row">
              <td>
                <span class="db-name">{{ m.address }}</span>
                <span v-if="!m.enabled" class="badge">Sign-in off</span>
                <small v-if="m.name" class="muted block">{{ m.name }}</small>
              </td>
              <td class="mail-usage">
                <form v-if="editingQuota === m.local" class="quota-form" @submit.prevent="saveQuota(m)">
                  <label :for="'quota-' + m.local" class="visually-hidden">Size limit in MB</label>
                  <input :id="'quota-' + m.local" v-model.number="quotaValue" type="number" min="0" step="256">
                  <span class="muted">MB</span>
                  <button class="button button-small" type="submit" :disabled="busy">Save</button>
                  <button class="button button-quiet" type="button" @click="editingQuota = null">Cancel</button>
                </form>
                <template v-else-if="usage(m)">
                  <span>{{ usage(m).label }}</span>
                  <div v-if="usage(m).pct !== null" class="meter" :data-level="usage(m).pct >= 85 ? 'high' : 'normal'"
                       role="meter" :aria-label="'Storage of ' + m.address" aria-valuemin="0" aria-valuemax="100"
                       :aria-valuenow="Math.round(usage(m).pct)">
                    <div class="meter-fill" :style="{ width: usage(m).pct.toFixed(1) + '%' }"></div>
                  </div>
                </template>
              </td>
              <td class="row-actions">
                <template v-if="confirmBox === m.local">
                  <span class="confirm-text">Delete it and all its mail?</span>
                  <button class="button button-danger button-small" type="button" :disabled="busy" @click="deleteMailbox(m)">Delete</button>
                  <button class="button button-quiet" type="button" @click="confirmBox = null">Cancel</button>
                </template>
                <template v-else>
                  <button class="button button-quiet" type="button" :disabled="busy" @click="newPassword(m)">New password</button>
                  <button class="button button-quiet" type="button" @click="startQuota(m)">Size limit</button>
                  <button class="button button-quiet" type="button" :disabled="busy" @click="setEnabled(m, !m.enabled)">{{ m.enabled ? "Turn off sign-in" : "Allow sign-in" }}</button>
                  <button class="button button-quiet danger-text" type="button" @click="confirmBox = m.local">Delete</button>
                </template>
              </td>
            </tr>
          </tbody>
        </table>
      </section>

      <section class="panel table-panel">
        <div class="table-head">
          <h2 class="table-title">Forwarders</h2>
          <button v-if="!addingAlias" class="button button-secondary button-small" type="button" @click="addingAlias = true; aliasError = ''">Add forwarder</button>
        </div>
        <form v-if="addingAlias" class="alias-form" @submit.prevent="addAlias">
          <p v-if="aliasError" class="alert" role="alert">{{ aliasError }}</p>
          <div class="form-grid form-grid-even">
            <div class="field">
              <label for="alias-local">Address</label>
              <div class="prefixed suffixed">
                <input id="alias-local" v-model.trim="alias.local" placeholder="sales" autocapitalize="none" spellcheck="false" required>
                <span class="prefix" aria-hidden="true">@{{ domain }}</span>
              </div>
            </div>
            <div class="field">
              <label for="alias-dest">Forward to</label>
              <input id="alias-dest" v-model.trim="alias.destinations" placeholder="info@example.com, me@gmail.com" autocapitalize="none" spellcheck="false" required>
            </div>
          </div>
          <p class="muted hint">Use * as the address for a catch-all: mail to any address without its own mailbox or forwarder.<span v-if="hasCatchAll"> This domain already has one.</span></p>
          <div class="actions">
            <button class="button" type="submit" :disabled="busy || !alias.local || !alias.destinations">Add forwarder</button>
            <button class="button button-quiet" type="button" @click="addingAlias = false">Cancel</button>
          </div>
        </form>
        <p v-if="!data.aliases.length && !addingAlias" class="muted table-empty">No forwarders. They send mail for an address on to one or more other addresses.</p>
        <table v-else-if="data.aliases.length" class="table db-table">
          <thead><tr><th scope="col">Address</th><th scope="col">Forwards to</th><th scope="col"><span class="visually-hidden">Actions</span></th></tr></thead>
          <tbody>
            <tr v-for="a in data.aliases" :key="a.local" class="static-row">
              <td class="db-name">{{ aliasAddress(a) }}</td>
              <td><span v-for="(d, i) in a.destinations" :key="d">{{ d }}<br v-if="i < a.destinations.length - 1"></span></td>
              <td class="row-actions">
                <template v-if="confirmAlias === a.local">
                  <button class="button button-danger button-small" type="button" :disabled="busy" @click="deleteAlias(a)">Delete</button>
                  <button class="button button-quiet" type="button" @click="confirmAlias = null">Cancel</button>
                </template>
                <button v-else class="button button-quiet danger-text" type="button" @click="confirmAlias = a.local">Delete</button>
              </td>
            </tr>
          </tbody>
        </table>
      </section>

      <section class="panel table-panel">
        <div class="table-head">
          <h2 class="table-title">DNS records</h2>
          <button v-if="data.dns_zone" class="button button-secondary button-small" type="button" :disabled="busy" @click="addDns">Add missing records to {{ data.dns_zone }}</button>
        </div>
        <p class="muted table-intro">
          Mail for {{ domain }} needs these records, so other servers deliver it here and trust what you send.
          <template v-if="!data.dns_zone">Add them where the domain's DNS is managed.</template>
        </p>
        <div v-if="dnsResult" class="table-intro">
          <p v-if="dnsResult.message" class="alert" role="alert">{{ dnsResult.message }}</p>
          <p v-if="dnsResult.added.length" class="notice" role="status">Added: {{ dnsResult.added.join(", ") }}.</p>
          <p v-if="dnsResult.skipped.length" class="muted">Already present, left as they are: {{ dnsResult.skipped.join(", ") }}.</p>
        </div>
        <table class="table records-table db-table">
          <thead><tr><th scope="col">Name</th><th scope="col">Type</th><th scope="col">Value</th></tr></thead>
          <tbody>
            <tr v-for="r in data.records" :key="r.name + r.type" class="static-row">
              <td class="rec-name">{{ recordName(r) }}</td>
              <td><span class="type-tag">{{ r.type }}</span></td>
              <td class="rec-content"><code class="secret">{{ r.priority !== undefined ? r.priority + " " : "" }}{{ r.content }}</code></td>
            </tr>
          </tbody>
        </table>
      </section>

      <section class="panel panel-danger zone-remove">
        <h2>Delete mail domain</h2>
        <p class="muted">All mailboxes of {{ domain }}, their mail and the forwarders are deleted. DNS records stay as they are.</p>
        <div class="actions">
          <template v-if="confirmDomain">
            <button class="button button-danger" type="button" :disabled="busy" @click="deleteDomain">Delete everything</button>
            <button class="button button-quiet" type="button" @click="confirmDomain = false">Cancel</button>
          </template>
          <button v-else class="button button-secondary danger-text" type="button" @click="confirmDomain = true">Delete mail domain</button>
        </div>
      </section>
    </template>
  `,
};
