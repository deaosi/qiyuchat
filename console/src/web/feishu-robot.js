(() => {
  "use strict";

  const monitor = document.querySelector("[data-feishu-monitor]");
  if (!monitor) return;
  monitor.classList.add("robot-monitor");
  monitor.innerHTML = `
    <section class="robot-card robot-summary" aria-labelledby="robot-connection-title">
      <div class="robot-card-head">
        <div>
          <div class="robot-title">
            <h2 id="robot-connection-title">连接状态</h2>
            <span class="robot-state" data-connection hidden></span>
          </div>
          <span class="robot-meta" data-observed hidden></span>
        </div>
        <button type="button" class="robot-button" data-refresh>刷新</button>
      </div>
    </section>
    <p class="robot-notice robot-muted" data-notice role="status" hidden></p>
    <div class="robot-content" data-content hidden></div>
  `;

  const refresh = monitor.querySelector("[data-refresh]");
  const connection = monitor.querySelector("[data-connection]");
  const observed = monitor.querySelector("[data-observed]");
  const notice = monitor.querySelector("[data-notice]");
  const content = monitor.querySelector("[data-content]");
  const labels = {
    connected: "连接正常", degraded: "需检查", unverified: "待核验",
    healthy: "运行正常", warning: "需检查", stale: "超时未上报",
    success: "成功", info: "信息", error: "失败",
  };
  const formatter = new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23",
  });
  let loading = false;
  let hasLoaded = false;

  function element(tag, className, value) {
    const result = document.createElement(tag);
    if (className) result.className = className;
    if (value !== undefined) result.textContent = String(value);
    return result;
  }

  function timestamp(value) {
    const date = typeof value === "string" ? new Date(value) : null;
    if (!date || !Number.isFinite(date.getTime())) return "—";
    const time = element("time", "", formatter.format(date));
    time.dateTime = date.toISOString();
    return time;
  }

  function interval(value) {
    if (!Number.isFinite(value) || value <= 0) return "—";
    if (value % 3600 === 0) return `${value / 3600} 小时`;
    if (value % 60 === 0) return `${value / 60} 分钟`;
    return `${value} 秒`;
  }

  function setState(target, status) {
    target.textContent = Object.hasOwn(labels, status) ? labels[status] : "待核验";
    target.dataset.state = status || "unverified";
    target.hidden = false;
    return target;
  }

  function facts(values) {
    const list = element("dl", "robot-facts");
    for (const [label, value] of values) {
      const pair = element("div");
      const description = element("dd");
      description.append(value ?? "—");
      pair.append(element("dt", "", label), description);
      list.append(pair);
    }
    return list;
  }

  function renderLane(lane) {
    const card = element("article", "robot-card robot-lane");
    const heading = element("div", "robot-card-head");
    heading.append(element("h3", "", lane.label), setState(element("span", "robot-state"), lane.status));
    card.append(heading);
    if (lane.message) card.append(element("p", "robot-meta", lane.message));
    const details = [
      ["同步间隔", interval(lane.interval_seconds)],
      ["最近运行", timestamp(lane.last_run_at)],
      ["最近核验成功", timestamp(lane.last_success_at)],
    ];
    if (lane.last_published_at) details.push(["最近写入飞书", timestamp(lane.last_published_at)]);
    card.append(facts(details));
    if (lane.counters?.length) {
      card.append(facts(lane.counters.map((counter) => [counter.label, counter.value])));
    }
    return card;
  }

  function renderLogs(logs) {
    const section = element("section", "robot-card robot-logs");
    section.append(element("h2", "", "最近同步日志"));
    if (!logs.length) {
      section.append(element("p", "robot-empty", "暂无同步日志。"));
      return section;
    }
    const wrap = element("div", "robot-table-wrap");
    wrap.tabIndex = 0;
    wrap.setAttribute("role", "region");
    wrap.setAttribute("aria-label", "最近同步日志");
    const table = element("table", "robot-log");
    table.setAttribute("aria-label", "最近同步日志");
    const head = table.createTHead().insertRow();
    for (const title of ["时间（北京时间）", "同步任务", "结果", "详情"]) {
      const th = element("th", "", title);
      th.scope = "col";
      head.append(th);
    }
    const body = table.createTBody();
    for (const log of logs) {
      const row = body.insertRow();
      row.insertCell().append(timestamp(log.timestamp));
      row.insertCell().textContent = log.label;
      row.insertCell().append(setState(element("span", "robot-state"), log.level));
      row.insertCell().textContent = log.message;
    }
    wrap.append(table);
    section.append(wrap);
    return section;
  }

  function renderSettings(settings) {
    const section = element("section", "robot-card robot-settings");
    section.append(element("h2", "", "同步设置"));
    const grid = element("div", "robot-setting-list");
    for (const lane of settings.lanes) {
      const card = element("article", "robot-setting");
      card.append(element("h3", "", lane.label), facts([
        ["运行方式", lane.mode],
        ["同步间隔", interval(lane.interval_seconds)],
        ["同步目标", lane.target_configured ? "已配置" : "未配置"],
      ]));
      grid.append(card);
    }
    section.append(grid);
    return section;
  }

  function render(data) {
    const lanes = element("div", "robot-lanes");
    for (const lane of data.lanes) lanes.append(renderLane(lane));
    content.replaceChildren(lanes, renderLogs(data.logs));
    if (data.settings?.lanes?.length) content.append(renderSettings(data.settings));
    setState(connection, data.status);
    observed.replaceChildren("最近上报（北京时间） ", timestamp(data.reported_at));
    observed.hidden = false;
    content.hidden = false;
  }

  function message(text, error = false) {
    notice.textContent = text;
    notice.hidden = !text;
    notice.toggleAttribute("data-error", error);
  }

  async function load() {
    if (loading || monitor.closest("[hidden]")) return;
    loading = true;
    refresh.disabled = true;
    refresh.textContent = "刷新中…";
    if (content.hidden) message("正在读取连接状态…");
    try {
      const response = await fetch("/api/feishu-robot/status", {
        credentials: "same-origin",
        cache: "no-store",
        headers: { Accept: "application/json" },
      });
      if (response.status === 401 || response.status === 403) {
        connection.hidden = true;
        observed.hidden = true;
        content.hidden = true;
        content.replaceChildren();
        message(response.status === 401
          ? "登录已失效，请返回语海 Voc 登录。"
          : "当前密钥没有飞书机器人访问权限。", true);
        return;
      }
      if (!response.ok) throw new Error("状态读取失败");
      const data = await response.json();
      if (!Array.isArray(data.lanes) || !Array.isArray(data.logs)) throw new Error("状态格式无效");
      render(data);
      hasLoaded = true;
      message("");
    } catch {
      if (content.hidden) message("暂时无法读取连接状态，请刷新重试。", true);
      else {
        setState(connection, "unverified");
        message("刷新失败，以下为上次读取的记录。", true);
      }
    } finally {
      loading = false;
      refresh.disabled = false;
      refresh.textContent = "刷新";
    }
  }

  refresh.addEventListener("click", load);
  window.addEventListener("hashchange", () => { if (!monitor.closest("[hidden]")) void load(); });
  window.addEventListener("pagehide", () => {
    connection.hidden = true;
    observed.hidden = true;
    content.hidden = true;
    content.replaceChildren();
  });
  window.addEventListener("pageshow", (event) => { if (event.persisted) void load(); });
  document.addEventListener("visibilitychange", () => {
    if (hasLoaded && document.visibilityState === "visible") void load();
  });
  void load();
})();
