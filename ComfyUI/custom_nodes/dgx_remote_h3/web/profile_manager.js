import { app } from "../../scripts/app.js";

const STATUS_URL = "/dgx-h3/profile/status";
const ACTION_URL = "/dgx-h3/profile/action";
const ACTIVE_POLL_MS = 2000;
const IDLE_POLL_MS = 10000;
const TERMINAL_STATES = new Set(["READY", "STOPPED", "FAILED_SAFE_STOPPED", "FAILED_UNSAFE_UNKNOWN"]);

async function readJson(response) {
  const payload = await response.json().catch(() => ({ error: "invalid_response" }));
  if (!response.ok) {
    if (response.status === 409) throw new Error("BUSY：当前生成不会被中断，请完成后再试。");
    throw new Error(`操作失败：${payload.error || response.status}`);
  }
  return payload;
}

function renderStatus(value, updatedAt = new Date()) {
  const identity = value.profile_identity || {};
  const state = value.state || "UNKNOWN";
  const marker = state === "READY" ? "🟢" : TERMINAL_STATES.has(state) ? "🔴" : "🟡";
  const healthy = value.healthy === true ? "正常" : value.healthy === false ? "异常" : "未知";
  return {
    state: `${marker} ${state}`,
    profile: value.profile || identity.name || "无",
    ranks: identity.rank_identities?.length === 2 ? "READY" : "未就绪",
    health: healthy,
    updated: updatedAt.toLocaleTimeString(),
  };
}

function pollDelay(state, actionInFlight) {
  return actionInFlight || !TERMINAL_STATES.has(state) ? ACTIVE_POLL_MS : IDLE_POLL_MS;
}

app.registerExtension({
  name: "dgx.remote.h3.profile-manager",
  async beforeRegisterNodeDef(nodeType, nodeData) {
    if (nodeData.name !== "DGXRemoteMiniMaxH3ProfileManager") return;
    const original = nodeType.prototype.onNodeCreated;
    nodeType.prototype.onNodeCreated = function () {
      original?.apply(this, arguments);
      const status = this.addWidget("text", "当前状态", "正在读取状态…", () => {});
      const profileStatus = this.addWidget("text", "当前 Profile", "读取中…", () => {});
      const rankStatus = this.addWidget("text", "双 Rank", "读取中…", () => {});
      const healthStatus = this.addWidget("text", "健康状态", "读取中…", () => {});
      const updatedStatus = this.addWidget("text", "最后更新", "读取中…", () => {});
      const statusWidgets = [status, profileStatus, rankStatus, healthStatus, updatedStatus];
      statusWidgets.forEach((widget) => { widget.serialize = false; });
      const applyStatus = (value) => {
        const rendered = renderStatus(value);
        status.value = rendered.state;
        profileStatus.value = rendered.profile;
        rankStatus.value = rendered.ranks;
        healthStatus.value = rendered.health;
        updatedStatus.value = rendered.updated;
      };
      let pollTimer = null;
      let requestInFlight = false;
      let actionInFlight = false;
      let removed = false;
      let lastState = "UNKNOWN";

      const schedulePoll = (delay) => {
        clearTimeout(pollTimer);
        if (!removed) pollTimer = setTimeout(refreshStatus, delay);
      };

      const refreshStatus = async () => {
        if (removed || requestInFlight) return;
        requestInFlight = true;
        let statusError = false;
        try {
          const value = await readJson(await fetch(STATUS_URL, { cache: "no-store" }));
          lastState = value.state || "UNKNOWN";
          applyStatus(value);
        } catch (error) {
          statusError = true;
          lastState = "UNKNOWN";
          status.value = "🔴 状态连接失败";
          profileStatus.value = String(error.message || error);
          rankStatus.value = "未知";
          healthStatus.value = "异常";
          updatedStatus.value = "将在 10 秒后重试";
        } finally {
          requestInFlight = false;
          this.setDirtyCanvas(true, true);
          schedulePoll(statusError ? IDLE_POLL_MS : pollDelay(lastState, actionInFlight));
        }
      };

      const handleVisibilityChange = () => {
        if (!document.hidden) {
          clearTimeout(pollTimer);
          refreshStatus();
        }
      };

      const button = this.addWidget("button", "立即执行 / 刷新", null, async () => {
        const profile = this.widgets.find((item) => item.name === "profile")?.value;
        const action = this.widgets.find((item) => item.name === "action")?.value;
        button.disabled = true;
        clearTimeout(pollTimer);
        try {
          if (action === "status") {
            await refreshStatus();
          } else {
            actionInFlight = true;
            status.value = "🟡 正在切换；模型冷加载可能需要数分钟…";
            refreshStatus();
            const response = await fetch(ACTION_URL, {
              method: "POST",
              headers: { "Content-Type": "application/json", "X-DGX-H3-Action": "explicit-user-action" },
              body: JSON.stringify({ action, profile }),
            });
            applyStatus(await readJson(response));
          }
        } catch (error) {
          status.value = String(error.message || error);
        } finally {
          actionInFlight = false;
          button.disabled = false;
          this.setDirtyCanvas(true, true);
          clearTimeout(pollTimer);
          refreshStatus();
        }
      });
      document.addEventListener("visibilitychange", handleVisibilityChange);
      const originalRemoved = this.onRemoved?.bind(this);
      this.onRemoved = () => {
        removed = true;
        clearTimeout(pollTimer);
        document.removeEventListener("visibilitychange", handleVisibilityChange);
        originalRemoved?.();
      };
      refreshStatus();
      this.setSize([Math.max(this.size[0], 420), Math.max(this.size[1], 360)]);
    };
  },
});
