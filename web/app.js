import { createApp, ref, computed, onMounted, onUnmounted } from "./vendor/vue.esm-browser.prod.js";
import { api, session, route, navigate, formatBytes, formatUptime } from "./js/core.js";
import { WebsitesView, WebsiteDetailView } from "./js/websites.js";
import { DatabasesView } from "./js/databases.js";
import { DnsView, DnsZoneView } from "./js/dns.js";

// ---------------------------------------------------------------------------
// Navigation
// ---------------------------------------------------------------------------

const SECTIONS = [
  { path: "/", label: "Dashboard" },
  { path: "/websites", label: "Websites" },
  { path: "/databases", label: "Databases" },
  { path: "/dns", label: "DNS" },
  {
    path: "/email",
    label: "Email",
    soon: true,
    plan: ["Mailboxes, forwarders and catch-alls", "DKIM, SPF and DMARC set up for you", "Spam filtering and webmail"],
  },
  {
    path: "/backups",
    label: "Backups",
    soon: true,
    plan: ["Scheduled backups to S3, Backblaze B2 or SFTP", "Restore a whole site or a single file"],
  },
  {
    path: "/wordpress",
    label: "WordPress",
    soon: true,
    plan: ["One-click install", "Staging copies and cloning", "Core, plugin and theme updates"],
  },
  {
    path: "/users",
    label: "Users",
    soon: true,
    plan: ["Accounts for your clients", "Hosting packages with limits", "Resellers"],
  },
];

// ---------------------------------------------------------------------------
// Views
// ---------------------------------------------------------------------------

const LoginView = {
  setup() {
    const username = ref("");
    const password = ref("");
    const error = ref("");
    const busy = ref(false);

    async function submit() {
      error.value = "";
      busy.value = true;
      try {
        const data = await api("/api/auth/login", {
          method: "POST",
          body: { username: username.value, password: password.value },
        });
        session.user = data.user;
        session.csrf = data.csrf;
        password.value = "";
      } catch (err) {
        error.value = err.message;
      } finally {
        busy.value = false;
      }
    }

    return { username, password, error, busy, submit };
  },
  template: `
    <main class="login">
      <section class="loom" aria-label="Tela">
        <div>
          <h1 class="wordmark">Tela</h1>
          <p>Websites, email and DNS on your own server.</p>
        </div>
        <small>Free and open source under the GPL.</small>
      </section>
      <div class="login-form-wrap">
        <form class="login-form" @submit.prevent="submit" novalidate>
          <h2>Sign in</h2>
          <p v-if="error" class="alert" role="alert">{{ error }}</p>
          <div class="field">
            <label for="username">Username</label>
            <input id="username" v-model="username" autocomplete="username"
                   autocapitalize="none" spellcheck="false" required autofocus>
          </div>
          <div class="field">
            <label for="password">Password</label>
            <input id="password" v-model="password" type="password"
                   autocomplete="current-password" required>
          </div>
          <button class="button" type="submit" :disabled="busy || !username || !password">
            {{ busy ? "Signing in…" : "Sign in" }}
          </button>
        </form>
      </div>
    </main>
  `,
};

const DashboardView = {
  setup() {
    const status = ref(null);
    const error = ref("");
    let timer;

    async function load() {
      try {
        status.value = await api("/api/system/status");
        error.value = "";
      } catch (err) {
        error.value = err.message;
      }
    }

    onMounted(() => {
      load();
      timer = setInterval(load, 10000);
    });
    onUnmounted(() => clearInterval(timer));

    const meters = computed(() => {
      const s = status.value;
      if (!s) return [];
      const loadPct = Math.min((s.load[0] / s.cpus) * 100, 100);
      return [
        {
          label: "CPU load",
          detail: `${s.load[0]} on ${s.cpus} ${s.cpus === 1 ? "core" : "cores"}`,
          pct: loadPct,
        },
        {
          label: "Memory",
          detail: `${formatBytes(s.memory.used)} of ${formatBytes(s.memory.total)}`,
          pct: (s.memory.used / s.memory.total) * 100,
        },
        {
          label: "Disk",
          detail: `${formatBytes(s.disk.used)} of ${formatBytes(s.disk.total)}`,
          pct: (s.disk.used / s.disk.total) * 100,
        },
      ];
    });

    return { status, error, meters, formatUptime };
  },
  template: `
    <header class="page-head">
      <h1>{{ status ? status.hostname : "Server" }}</h1>
      <p v-if="status">Up {{ formatUptime(status.uptime) }}</p>
    </header>
    <p v-if="error" class="alert" role="alert">{{ error }}</p>
    <p v-else-if="!status" class="loading">Loading server status…</p>
    <div v-if="status" class="dashboard">
      <section class="panel">
        <h2>Resources</h2>
        <ul class="meters">
          <li v-for="m in meters" :key="m.label">
            <div class="meter-label"><span>{{ m.label }}</span><span>{{ m.detail }}</span></div>
            <div class="meter" :data-level="m.pct >= 85 ? 'high' : 'normal'"
                 role="meter" :aria-label="m.label" aria-valuemin="0" aria-valuemax="100"
                 :aria-valuenow="Math.round(m.pct)">
              <div class="meter-fill" :style="{ width: m.pct.toFixed(1) + '%' }"></div>
            </div>
          </li>
        </ul>
      </section>
      <section class="panel">
        <h2>Services</h2>
        <ul class="services">
          <li v-for="svc in status.services" :key="svc.unit">
            <div>{{ svc.name }}<small>{{ svc.unit }}</small></div>
            <span class="state" :data-state="svc.state">{{ svc.state === "active" ? "Running" : svc.state === "unknown" ? "Unknown" : "Stopped" }}</span>
          </li>
        </ul>
      </section>
      <section class="panel panel-wide">
        <h2>System</h2>
        <dl class="facts">
          <div><dt>Operating system</dt><dd>{{ status.os }}</dd></div>
          <div><dt>Kernel</dt><dd>{{ status.kernel }}</dd></div>
          <div><dt>Load average</dt><dd>{{ status.load.join(" / ") }}</dd></div>
          <div><dt>Tela version</dt><dd>{{ status.panel_version }}</dd></div>
        </dl>
      </section>
    </div>
  `,
};

const PlannedView = {
  props: ["section"],
  template: `
    <header class="page-head"><h1>{{ section.label }}</h1></header>
    <section class="panel placeholder">
      <p>This part of Tela isn't built yet. It will let you:</p>
      <ul><li v-for="item in section.plan" :key="item">{{ item }}</li></ul>
    </section>
  `,
};

const ShellView = {
  components: {
    DashboardView, PlannedView, WebsitesView, WebsiteDetailView, DatabasesView, DnsView, DnsZoneView,
  },
  setup() {
    const current = computed(
      () =>
        SECTIONS.find((s) => s.path !== "/" && (route.value === s.path || route.value.startsWith(s.path + "/"))) ||
        SECTIONS[0],
    );
    const detail = computed(() => {
      const match = route.value.match(/^\/websites\/([^/]+)$/);
      return match ? decodeURIComponent(match[1]) : null;
    });
    const dnsZone = computed(() => {
      const match = route.value.match(/^\/dns\/([^/]+)$/);
      return match ? decodeURIComponent(match[1]) : null;
    });
    const busy = ref(false);

    async function signOut() {
      busy.value = true;
      try {
        await api("/api/auth/logout", { method: "POST" });
      } finally {
        session.user = null;
        session.csrf = "";
        busy.value = false;
        navigate("/");
      }
    }

    const roleNames = { admin: "Administrator", reseller: "Reseller", user: "Client" };
    return { sections: SECTIONS, current, detail, dnsZone, route, session, navigate, signOut, busy, roleNames };
  },
  template: `
    <div class="shell">
      <aside class="sidebar">
        <a class="sidebar-brand" href="/" @click.prevent="navigate('/')">
          <img src="/static/favicon.svg" alt="">Tela
        </a>
        <nav aria-label="Main">
          <ul class="nav">
            <li v-for="s in sections" :key="s.path">
              <a :href="s.path" :aria-current="current.path === s.path ? 'page' : null"
                 @click.prevent="navigate(s.path)">
                {{ s.label }}<span v-if="s.soon" class="soon">Soon</span>
              </a>
            </li>
          </ul>
        </nav>
        <div class="sidebar-user">
          <div><strong>{{ session.user.username }}</strong><span>{{ roleNames[session.user.role] }}</span></div>
          <button class="button button-quiet" type="button" :disabled="busy" @click="signOut">Sign out</button>
        </div>
      </aside>
      <main class="main">
        <DashboardView v-if="current.path === '/'" />
        <WebsiteDetailView v-else-if="detail" :key="detail" :domain="detail" />
        <WebsitesView v-else-if="current.path === '/websites'" />
        <DatabasesView v-else-if="current.path === '/databases'" />
        <DnsZoneView v-else-if="dnsZone" :key="dnsZone" :zone="dnsZone" />
        <DnsView v-else-if="current.path === '/dns'" />
        <PlannedView v-else :section="current" />
      </main>
    </div>
  `,
};

const App = {
  components: { LoginView, ShellView },
  setup() {
    onMounted(async () => {
      try {
        const data = await api("/api/auth/me");
        session.user = data.user;
        session.csrf = data.csrf;
      } catch {
        // not signed in
      } finally {
        session.checked = true;
      }
    });
    return { session };
  },
  template: `
    <template v-if="session.checked">
      <ShellView v-if="session.user" />
      <LoginView v-else />
    </template>
  `,
};

createApp(App).mount("#app");
