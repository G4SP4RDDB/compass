import { fetchMeasuredDelays, fetchDexConfig, saveDexConfig } from "../api/client";
import type { DexOperationalParams, DexMeasuredDelays } from "../types/graphData";
import type { DexConfigResponse } from "../types/api";

// Panel "Config" : édition des frais/délais de dépôt/retrait par DEX ET PAR
// CHAIN, enregistrée directement dans connectors/dex_operational_params.json via le
// serveur de visualization/server.py (GET/POST /api/config) — plus de
// copier-coller de JSON, quelqu'un qui n'a que cette page en production peut
// modifier la config sans toucher au code. localStorage ne sert plus que de
// repli : page ouverte en fichier local sans serveur, ou serveur injoignable.
const CONFIG_STORAGE_KEY = "compass_dex_operational_params";
const CONFIG_FIELDS: (keyof DexOperationalParams)[] = [
  "withdrawFeeUsd", "withdrawDelaySeconds", "minWithdrawUsd", "depositFeeUsd", "depositDelaySeconds", "minDepositUsd",
];

function loadStoredConfig(): DexConfigResponse {
  try {
    const raw = localStorage.getItem(CONFIG_STORAGE_KEY);
    return raw ? JSON.parse(raw) : {};
  } catch {
    return {};
  }
}

function saveStoredConfig(config: DexConfigResponse): void {
  try {
    localStorage.setItem(CONFIG_STORAGE_KEY, JSON.stringify(config));
  } catch {
    // Repli best-effort seulement (quota dépassé, mode privé, ...) : rien à
    // faire, la source de vérité reste le serveur.
  }
}

// dexConfig[dexName][chainName] = {withdrawFeeUsd, withdrawDelaySeconds,
// depositFeeUsd, depositDelaySeconds} — un frais/délai par (DEX, chain), pas
// juste par DEX (voir DEX.withdrawFeeUsdByChain et consorts côté Python).
const dexConfig: Record<string, Record<string, DexOperationalParams>> = {};
let configServerAvailable = true;

// Délais MESURÉS par compass_test (moyenne des derniers runs live, voir
// compass_test/calibration.py) : measuredDelays[dexName][chainName] =
// {withdrawDelaySeconds: {meanSeconds, n, stdSeconds, ...}|null,
//  depositDelaySeconds: ...}. Pré-rempli depuis GRAPH_DATA (valeurs au
// moment du build du graphe, seule source hors serveur), rafraîchi via
// GET /api/measured-delays quand le serveur répond (un run live lancé
// depuis "Test This Edge" reconstruit le fichier sans rebuild du graphe).
const MEASURED_FIELDS: (keyof DexOperationalParams)[] = ["withdrawDelaySeconds", "depositDelaySeconds"];
const measuredDelays: Record<string, Record<string, DexMeasuredDelays>> = {};
for (const dex of GRAPH_DATA.dexNodes) {
  measuredDelays[dex.name] = dex.measuredDelays || {};
}

async function refreshMeasuredDelays(): Promise<void> {
  try {
    const fromServer = await fetchMeasuredDelays();
    if (!fromServer) return;
    for (const dexName of Object.keys(measuredDelays)) {
      const chains = fromServer[dexName] || {};
      for (const chain of Object.keys(measuredDelays[dexName]!)) {
        const entry = chains[chain] || { withdrawDelaySeconds: null, depositDelaySeconds: null };
        measuredDelays[dexName]![chain] = {
          withdrawDelaySeconds: entry.withdrawDelaySeconds || null,
          depositDelaySeconds: entry.depositDelaySeconds || null,
        };
      }
    }
  } catch {
    // Pas de serveur : on garde les valeurs embarquées dans GRAPH_DATA.
  }
}

function buildDexConfig(overrides: DexConfigResponse): void {
  for (const dex of GRAPH_DATA.dexNodes) {
    dexConfig[dex.name] = {};
    const serverChains = dex.operationalParams || {};
    const overrideChains = overrides[dex.name] || {};
    for (const chain of dex.chains) {
      const defaults: DexOperationalParams = { withdrawFeeUsd: 0, withdrawDelaySeconds: 0, minWithdrawUsd: 5, depositFeeUsd: 0, depositDelaySeconds: 0, minDepositUsd: 0 };
      dexConfig[dex.name]![chain] = Object.assign(defaults, serverChains[chain] || {}, overrideChains[chain] || {});
    }
  }
}

function setConfigStatus(text: string, cssClass?: string): void {
  const el = document.getElementById("configStatus")!;
  el.textContent = text;
  el.className = "config-status" + (cssClass ? " " + cssClass : "");
}

export async function initDexConfig(): Promise<void> {
  await refreshMeasuredDelays();
  try {
    buildDexConfig(await fetchDexConfig());
    saveStoredConfig(dexConfig); // repli à jour si le serveur devient injoignable plus tard
  } catch {
    configServerAvailable = false;
    buildDexConfig(loadStoredConfig());
    setConfigStatus(
      "Not connected to the config server — showing this browser's last known values, edits won't be saved. Serve this page with `python -m visualization.server`.",
      "config-status-offline"
    );
  }
  renderConfig();
}

let saveTimer: ReturnType<typeof setTimeout> | null = null;
// `force` : ignore le configServerAvailable=false mis en cache par un échec
// précédent (voir initDexConfig) et retente quand même — pour le bouton
// "Save" ci-dessous, qui doit pouvoir rattraper le coup si le serveur a été
// démarré APRÈS le chargement de la page, sans que l'utilisateur ait à
// recharger. L'auto-save au fil de la frappe (voir renderConfig), lui,
// continue de respecter le cache pour ne pas marteler un serveur qu'on sait
// déjà injoignable.
async function persistConfig(force = false): Promise<void> {
  saveStoredConfig(dexConfig);
  if (!configServerAvailable && !force) return;
  setConfigStatus("Saving…");
  try {
    await saveDexConfig(dexConfig);
    configServerAvailable = true;
    setConfigStatus("Saved", "config-status-ok");
  } catch {
    configServerAvailable = false;
    setConfigStatus("Save failed — check the config server", "config-status-error");
  }
}

function renderConfig(): void {
  const dexes = [...GRAPH_DATA.dexNodes].sort((a, b) => a.name.localeCompare(b.name));
  const rows: string[] = [];
  for (const dex of dexes) {
    for (const chain of dex.chains) {
      const c = dexConfig[dex.name]![chain]!;
      const measuredChain = (measuredDelays[dex.name] || {})[chain] || {};
      const cells = CONFIG_FIELDS.map(field => {
        const m = MEASURED_FIELDS.includes(field) ? (measuredChain as Record<string, DexMeasuredDelays[keyof DexMeasuredDelays]>)[field] : null;
        if (!m) {
          const note = MEASURED_FIELDS.includes(field)
            ? `<div class="cfg-measured">no live run yet · config in effect</div>`
            : "";
          return `<td class="${MEASURED_FIELDS.includes(field) ? "cfg-delay" : ""}"><input type="number" step="any" data-field="${field}" value="${c[field]}">${note}</td>`;
        }
        const std = m.n >= 2 ? ` ±${m.stdSeconds.toFixed(1)}s` : "";
        const nClass = m.n < 3 ? " cfg-measured-lown" : "";
        return `<td class="cfg-delay"><input type="number" step="any" data-field="${field}" value="${c[field]}" class="cfg-overridden" title="Overridden by the measured mean below (see connectors/dex_measured_delays.json)">` +
          `<div class="cfg-measured"><span class="cfg-measured-active">measured ${m.meanSeconds.toFixed(1)}s</span>${std} · <span class="${nClass}">n=${m.n}</span></div></td>`;
      }).join("");
      rows.push(
        `<tr data-dex="${dex.name}" data-chain="${chain}"><td class="cfg-name">${dex.name}</td><td class="cfg-chain">${chain}</td>${cells}</tr>`
      );
    }
  }
  document.getElementById("configBody")!.innerHTML = rows.join("");

  document.getElementById("configBody")!.querySelectorAll("input").forEach(input => {
    input.addEventListener("input", () => {
      const tr = input.closest("tr") as HTMLElement;
      const name = tr.dataset.dex!;
      const chain = tr.dataset.chain!;
      const value = parseFloat((input as HTMLInputElement).value);
      (dexConfig[name]![chain] as unknown as Record<string, number>)[(input as HTMLInputElement).dataset.field!] = Number.isFinite(value) ? value : 0;
      if (saveTimer) clearTimeout(saveTimer);
      saveTimer = setTimeout(() => persistConfig(), 400);
    });
  });
}

export function bindConfigSaveButton(): void {
  document.getElementById("configSaveBtn")!.addEventListener("click", () => {
    if (saveTimer) clearTimeout(saveTimer);
    persistConfig(true);
  });
}
