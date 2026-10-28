import { ref, reactive, computed, onMounted } from "../vendor/vue.esm-browser.prod.js";
import { api, session, navigate } from "./core.js";

const PROVIDER_LABEL = { local: "Tela DNS", cloudflare: "Cloudflare" };

const TTL_OPTIONS = [
  { value: 300, label: "5 minutes" },
  { value: 3600, label: "1 hour" },
  { value: 14400, label: "4 hours" },
  { value: 86400, label: "1 day" },
];

const CONTENT_HINT = {
  A: "203.0.113.10",
  AAAA: "2001:db8::10",
  CNAME: "target.example.com",
  MX: "mail.example.com",
  TXT: "v=spf1 a mx ~all",
  NS: "ns1.example.com",
  SRV: "weight port target, e.g. 5 5060 sip.example.com",
  CAA: '0 issue "letsencrypt.org"',
};

// ---------------------------------------------------------------------------
// Settings
// ---------------------------------------------------------------------------

const DnsSettings = {
  props: ["status"],
  emits: ["saved"],
  setup(props, { emit }) {
    const s = computed(() => props.status.settings);
    const initial = props.status.settings;
    const form = reactive({
      public_ipv4: initial.public_ipv4,
      public_ipv6: initial.public_ipv6,
      nameservers: initial.nameservers.join("\n"),
    });
    const token = ref("");
    const saving = ref(false);
    const connecting = ref(false);
    const error = ref("");
    const done = ref("");

    async function save() {
      saving.value = true;
      error.value = done.value = "";
      try {
        await api("/api/dns/settings", {
          method: "PUT",
          body: {
            public_ipv4: form.public_ipv4,
            public_ipv6: form.public_ipv6,
            nameservers: form.nameservers.split(/[\s,]+/).filter(Boolean),
          },
        });
        done.value = "Settings saved.";
        emit("saved");
      } catch (err) {
        error.value = err.message;
      } finally {
        saving.value = false;
      }
    }

    async function setToken(value) {
      connecting.value = true;
      error.value = done.value = "";
      try {
        await api("/api/dns/settings", { method: "PUT", body: { cloudflare_token: value } });
        token.value = "";
        done.value = value ? "Cloudflare connected." : "Cloudflare disconnected.";
        emit("saved");
      } catch (err) {
        error.value = err.message;
      } finally {
        connecting.value = false;
      }
    }

    return { form, token, saving, connecting, error, done, save, setToken, s };
  },
  template: `
    <section class="panel dns-settings">
      <h2>Settings</h2>
      <p v-if="error" class="alert" role="alert">{{ error }}</p>
      <p v-if="done" class="notice" role="status">{{ done }}</p>
      <form class="settings-grid" @submit.prevent="save">
        <div class="field">
          <label for="dns-ipv4">Server's public IPv4</label>
          <input id="dns-ipv4" v-model.trim="form.public_ipv4" placeholder="203.0.113.10" inputmode="decimal">
        </div>
        <div class="field">
          <label for="dns-ipv6">Server's public IPv6 <span class="muted">(optional)</span></label>
          <input id="dns-ipv6" v-model.trim="form.public_ipv6" placeholder="2001:db8::10">
        </div>
        <div class="field span-2">
          <label for="dns-ns">Nameservers for Tela DNS <span class="muted">(one per line)</span></label>
          <textarea id="dns-ns" v-model="form.nameservers" rows="2" placeholder="ns1.example.com&#10;ns2.example.com" spellcheck="false"></textarea>
        </div>
        <p class="muted hint span-2">New websites get A records pointing at the public IP when their domain has a zone here. Behind a router, that's your router's public IP.</p>
        <div class="actions span-2">
          <button class="button" type="submit" :disabled="saving">{{ saving ? "Saving…" : "Save settings" }}</button>
        </div>
      </form>

      <div class="cloudflare-box">
        <h3>Cloudflare</h3>
        <template v-if="s.cloudflare_connected">
          <p><span class="state" data-state="active">Connected</span> <span class="muted">with token {{ s.cloudflare_token_hint }}</span></p>
          <div class="actions">
            <button class="button button-quiet danger-text" type="button" :disabled="connecting" @click="setToken('')">Disconnect</button>
          </div>
        </template>
        <form v-else class="inline-form" @submit.prevent="setToken(token)">
          <div class="field">
            <label for="cf-token">API token</label>
            <input id="cf-token" v-model.trim="token" type="password" autocomplete="off" spellcheck="false">
          </div>
          <button class="button button-secondary" type="submit" :disabled="connecting || !token">{{ connecting ? "Checking…" : "Connect" }}</button>
          <p class="muted hint full">Create one in Cloudflare under My Profile, API Tokens, with "Zone: Read" and "DNS: Edit" permissions.</p>
        </form>
      </div>
    </section>
  `,
};

// ---------------------------------------------------------------------------
// Zones
// ---------------------------------------------------------------------------

export const DnsView = {
  components: { DnsSettings },
  setup() {
    const status = ref(null);
    const zones = ref(null);
    const error = ref("");
    const isAdmin = computed(() => session.user?.role === "admin");
    const showSettings = ref(false);

    const adding = ref(false);
    const cfZones = ref([]);
    const form = reactive({ provider: "", zone: "", cf_zone_id: "", with_defaults: true });
    const formError = ref("");
    const busy = ref(false);

    async function load() {
      try {
        const [st, z] = await Promise.all([api("/api/dns/status"), api("/api/dns/zones")]);
        status.value = st;
        zones.value = z.zones;
        error.value = "";
      } catch (err) {
        error.value = err.message;
      }
    }

    const providers = computed(() => {
      if (!status.value) return [];
      const list = [];
      if (status.value.settings.cloudflare_connected) list.push("cloudflare");
      if (status.value.local_available) list.push("local");
      return list;
    });

    async function openForm() {
      adding.value = true;
      formError.value = "";
      form.provider = providers.value[0] || "";
      if (providers.value.includes("cloudflare")) {
        try {
          cfZones.value = (await api("/api/dns/cloudflare/zones")).zones.filter((z) => !z.linked);
          form.cf_zone_id = cfZones.value[0]?.id || "";
        } catch (err) {
          formError.value = err.message;
        }
      }
    }

    async function create() {
      busy.value = true;
      formError.value = "";
      const zone =
        form.provider === "cloudflare"
          ? cfZones.value.find((z) => z.id === form.cf_zone_id)?.name
          : form.zone;
      try {
        await api("/api/dns/zones", {
          method: "POST",
          body: {
            zone,
            provider: form.provider,
            cf_zone_id: form.provider === "cloudflare" ? form.cf_zone_id : null,
            with_defaults: form.with_defaults,
          },
        });
        adding.value = false;
        navigate(`/dns/${zone}`);
      } catch (err) {
        formError.value = err.message;
      } finally {
        busy.value = false;
      }
    }

    onMounted(load);
    return {
      status, zones, error, isAdmin, showSettings, adding, cfZones, form, formError, busy,
      providers, openForm, create, load, navigate, PROVIDER_LABEL,
    };
  },
  template: `
    <header class="page-head">
      <h1>DNS</h1>
      <div class="actions" v-if="isAdmin && status">
        <button class="button button-quiet" type="button" @click="showSettings = !showSettings">{{ showSettings ? "Hide settings" : "Settings" }}</button>
        <button v-if="!adding && providers.length && zones && zones.length" class="button" type="button" @click="openForm">Add zone</button>
      </div>
    </header>

    <p v-if="error" class="alert" role="alert">{{ error }}</p>
    <p v-else-if="!status" class="loading">Loading DNS…</p>

    <template v-if="status">
      <DnsSettings v-if="isAdmin && (showSettings || !providers.length || (zones && !zones.length))" :status="status" @saved="load" />

      <form v-if="adding" class="panel form-panel" @submit.prevent="create">
        <h2>Add a zone</h2>
        <p v-if="formError" class="alert" role="alert">{{ formError }}</p>
        <fieldset class="choice" v-if="providers.length > 1">
          <legend>Where the records live</legend>
          <label v-for="p in providers" :key="p" class="check">
            <input type="radio" v-model="form.provider" :value="p">
            <span>{{ p === "cloudflare" ? "Cloudflare (your domain uses Cloudflare's nameservers)" : "Tela DNS (your domain uses this server's nameservers)" }}</span>
          </label>
        </fieldset>
        <div class="field" v-if="form.provider === 'cloudflare'">
          <label for="cf-zone">Cloudflare zone</label>
          <select id="cf-zone" v-model="form.cf_zone_id" required>
            <option v-for="z in cfZones" :key="z.id" :value="z.id">{{ z.name }}</option>
          </select>
          <p v-if="!cfZones.length" class="muted hint">All zones in this Cloudflare account are already added.</p>
        </div>
        <div class="field" v-else>
          <label for="zone-name">Domain</label>
          <input id="zone-name" v-model.trim="form.zone" placeholder="example.com" autocapitalize="none" spellcheck="false" required>
        </div>
        <label class="check" v-if="status.settings.public_ipv4 || status.settings.public_ipv6">
          <input type="checkbox" v-model="form.with_defaults">
          <span>Point the domain and www at this server ({{ status.settings.public_ipv4 || status.settings.public_ipv6 }}) if they have no record yet</span>
        </label>
        <div class="actions">
          <button class="button" type="submit" :disabled="busy || (form.provider === 'cloudflare' ? !form.cf_zone_id : !form.zone)">{{ busy ? "Adding zone…" : "Add zone" }}</button>
          <button class="button button-quiet" type="button" @click="adding = false" :disabled="busy">Cancel</button>
        </div>
      </form>

      <section v-if="zones && !zones.length && !adding" class="panel empty">
        <h2>No DNS zones yet</h2>
        <template v-if="!isAdmin"><p>Your administrator hasn't set up DNS for your domains.</p></template>
        <template v-else-if="providers.length">
          <p>Manage the records of a domain here. New websites then get their A records automatically.</p>
          <button class="button" type="button" @click="openForm">Add your first zone</button>
        </template>
        <p v-else>Connect Cloudflare in the settings above, or install Tela's own DNS server by running the installer with <code>--with-dns-server</code>.</p>
      </section>

      <section v-else-if="zones && zones.length" class="panel table-panel">
        <table class="table">
          <thead><tr><th scope="col">Zone</th><th scope="col">Provider</th><th scope="col">Records</th></tr></thead>
          <tbody>
            <tr v-for="z in zones" :key="z.zone" @click="navigate('/dns/' + z.zone)">
              <td><a :href="'/dns/' + z.zone" @click.prevent.stop="navigate('/dns/' + z.zone)">{{ z.zone }}</a></td>
              <td><span class="badge" :data-provider="z.provider">{{ PROVIDER_LABEL[z.provider] }}</span></td>
              <td>{{ z.records === null ? "—" : z.records }}</td>
            </tr>
          </tbody>
        </table>
      </section>
    </template>
  `,
};

// ---------------------------------------------------------------------------
// Records of one zone
// ---------------------------------------------------------------------------

function emptyRecord() {
  return { id: null, type: "A", name: "", content: "", ttl: null, priority: 10, proxied: false };
}

export const DnsZoneView = {
  props: ["zone"],
  setup(props) {
    const data = ref(null);
    const error = ref("");
    const notice = ref("");
    const isAdmin = computed(() => session.user?.role === "admin");
    const editing = ref(null); // record being added or edited
    const formError = ref("");
    const busy = ref(false);
    const confirming = ref(null);
    const confirmZone = ref(false);

    const cloudflare = computed(() => data.value?.provider === "cloudflare");
    const ttlOptions = computed(() =>
      cloudflare.value ? [{ value: 1, label: "Auto" }, ...TTL_OPTIONS] : TTL_OPTIONS,
    );

    async function load() {
      try {
        data.value = await api(`/api/dns/zones/${props.zone}/records`);
        error.value = "";
      } catch (err) {
        error.value = err.message;
      }
    }

    function startAdd() {
      editing.value = { ...emptyRecord(), ttl: cloudflare.value ? 1 : 3600 };
      formError.value = "";
    }
    function startEdit(record) {
      editing.value = {
        ...record,
        name: record.name === "@" ? "" : record.name,
        priority: record.priority ?? 10,
      };
      formError.value = "";
    }

    async function save() {
      busy.value = true;
      formError.value = "";
      const r = editing.value;
      const body = {
        name: r.name || "@",
        type: r.type,
        content: r.content,
        ttl: r.ttl,
        priority: ["MX", "SRV"].includes(r.type) ? Number(r.priority) : null,
        proxied: cloudflare.value && ["A", "AAAA", "CNAME"].includes(r.type) ? r.proxied : false,
      };
      try {
        if (r.id) {
          await api(`/api/dns/zones/${props.zone}/records/${r.id}`, { method: "PUT", body });
          notice.value = "Record saved.";
        } else {
          await api(`/api/dns/zones/${props.zone}/records`, { method: "POST", body });
          notice.value = "Record added.";
        }
        editing.value = null;
        await load();
      } catch (err) {
        formError.value = err.message;
      } finally {
        busy.value = false;
      }
    }

    async function remove(record) {
      busy.value = true;
      error.value = "";
      try {
        await api(`/api/dns/zones/${props.zone}/records/${record.id}`, { method: "DELETE" });
        confirming.value = null;
        notice.value = "Record deleted.";
        await load();
      } catch (err) {
        error.value = err.message;
      } finally {
        busy.value = false;
      }
    }

    async function removeZone() {
      busy.value = true;
      try {
        await api(`/api/dns/zones/${props.zone}`, { method: "DELETE" });
        navigate("/dns");
      } catch (err) {
        error.value = err.message;
        busy.value = false;
      }
    }

    function displayName(name) {
      return name === "@" ? props.zone : `${name}.${props.zone}`;
    }
    function ttlLabel(ttl) {
      if (ttl === 1) return "Auto";
      const option = TTL_OPTIONS.find((o) => o.value === ttl);
      return option ? option.label : `${ttl}s`;
    }

    onMounted(load);
    return {
      data, error, notice, isAdmin, editing, formError, busy, confirming, confirmZone, cloudflare,
      ttlOptions, startAdd, startEdit, save, remove, removeZone, displayName, ttlLabel, navigate,
      CONTENT_HINT, PROVIDER_LABEL, types: Object.keys(CONTENT_HINT),
    };
  },
  template: `
    <nav class="crumbs" aria-label="Breadcrumb"><a href="/dns" @click.prevent="navigate('/dns')">DNS</a></nav>
    <p v-if="error" class="alert" role="alert">{{ error }}</p>
    <p v-else-if="!data" class="loading">Loading records…</p>
    <template v-if="data">
      <header class="page-head">
        <h1>{{ zone }}</h1>
        <div class="actions">
          <span class="badge" :data-provider="data.provider">{{ PROVIDER_LABEL[data.provider] }}</span>
          <button v-if="!editing" class="button" type="button" @click="startAdd">Add record</button>
        </div>
      </header>
      <p v-if="notice" class="notice" role="status">{{ notice }}</p>

      <form v-if="editing" class="panel form-panel record-form" @submit.prevent="save">
        <h2>{{ editing.id ? "Edit record" : "Add a record" }}</h2>
        <p v-if="formError" class="alert" role="alert">{{ formError }}</p>
        <div class="record-grid">
          <div class="field">
            <label for="rec-type">Type</label>
            <select id="rec-type" v-model="editing.type">
              <option v-for="t in types" :key="t" :value="t">{{ t }}</option>
            </select>
          </div>
          <div class="field name-field">
            <label for="rec-name">Name</label>
            <div class="prefixed suffixed">
              <input id="rec-name" v-model.trim="editing.name" autocapitalize="none" spellcheck="false" aria-describedby="rec-name-help">
              <span class="prefix" aria-hidden="true">.{{ zone }}</span>
            </div>
          </div>
          <div class="field" v-if="editing.type === 'MX' || editing.type === 'SRV'">
            <label for="rec-prio">Priority</label>
            <input id="rec-prio" v-model.number="editing.priority" type="number" min="0" max="65535">
          </div>
          <div class="field">
            <label for="rec-ttl">TTL</label>
            <select id="rec-ttl" v-model.number="editing.ttl">
              <option v-for="o in ttlOptions" :key="o.value" :value="o.value">{{ o.label }}</option>
            </select>
          </div>
          <div class="field span-all">
            <label for="rec-content">{{ editing.type === "TXT" ? "Text" : "Value" }}</label>
            <textarea v-if="editing.type === 'TXT'" id="rec-content" v-model="editing.content" rows="3" :placeholder="CONTENT_HINT.TXT" spellcheck="false"></textarea>
            <input v-else id="rec-content" v-model.trim="editing.content" :placeholder="CONTENT_HINT[editing.type]" autocapitalize="none" spellcheck="false" required>
          </div>
        </div>
        <label class="check" v-if="cloudflare && ['A', 'AAAA', 'CNAME'].includes(editing.type)">
          <input type="checkbox" v-model="editing.proxied">
          <span>Proxy through Cloudflare (orange cloud)</span>
        </label>
        <p id="rec-name-help" class="muted hint">Leave the name empty for {{ zone }} itself. Use * for a wildcard.</p>
        <div class="actions">
          <button class="button" type="submit" :disabled="busy || !editing.content">{{ busy ? "Saving…" : editing.id ? "Save record" : "Add record" }}</button>
          <button class="button button-quiet" type="button" @click="editing = null" :disabled="busy">Cancel</button>
        </div>
      </form>

      <section v-if="!data.records.length && !editing" class="panel empty">
        <h2>No records yet</h2>
        <p>Add an A record to point {{ zone }} at a server.</p>
        <button class="button" type="button" @click="startAdd">Add a record</button>
      </section>

      <section v-else-if="data.records.length" class="panel table-panel">
        <table class="table records-table">
          <thead>
            <tr><th scope="col">Name</th><th scope="col">Type</th><th scope="col">Value</th><th scope="col">TTL</th><th scope="col"><span class="visually-hidden">Actions</span></th></tr>
          </thead>
          <tbody>
            <tr v-for="r in data.records" :key="r.id" class="static-row">
              <td class="rec-name">{{ displayName(r.name) }}</td>
              <td><span class="type-tag">{{ r.type }}</span></td>
              <td class="rec-content">
                <code>{{ r.priority !== null && r.priority !== undefined ? r.priority + " " : "" }}{{ r.content }}</code>
                <small v-if="r.proxied" class="proxied">Proxied</small>
              </td>
              <td>{{ ttlLabel(r.ttl) }}</td>
              <td class="row-actions">
                <template v-if="confirming === r.id">
                  <button class="button button-danger button-small" type="button" :disabled="busy" @click="remove(r)">Delete</button>
                  <button class="button button-quiet" type="button" @click="confirming = null">Cancel</button>
                </template>
                <template v-else>
                  <button class="button button-quiet" type="button" @click="startEdit(r)">Edit</button>
                  <button class="button button-quiet danger-text" type="button" @click="confirming = r.id">Delete</button>
                </template>
              </td>
            </tr>
          </tbody>
        </table>
      </section>

      <section v-if="isAdmin" class="panel panel-danger zone-remove">
        <h2>{{ cloudflare ? "Remove from Tela" : "Delete zone" }}</h2>
        <p class="muted">{{ cloudflare ? "Tela stops managing this zone. The zone and its records stay in Cloudflare." : "This server stops answering for " + zone + " and all its records are deleted." }}</p>
        <div class="actions">
          <template v-if="confirmZone">
            <button class="button button-danger" type="button" :disabled="busy" @click="removeZone">{{ cloudflare ? "Remove" : "Delete zone" }}</button>
            <button class="button button-quiet" type="button" @click="confirmZone = false">Cancel</button>
          </template>
          <button v-else class="button button-secondary danger-text" type="button" @click="confirmZone = true">{{ cloudflare ? "Remove from Tela" : "Delete zone" }}</button>
        </div>
      </section>
    </template>
  `,
};
