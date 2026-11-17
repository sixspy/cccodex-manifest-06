(() => {
  const view = document.getElementById("view");
  const msgEl = document.getElementById("msg");
  let tab = "batches";
  let meta = null;
  let batches = [];
  let selected = null;
  let detail = null;
  let reports = [];
  let runs = [];
  let activeRun = null;
  let form = { object_key: "", content_hash: "", byte_size: "100" };
  let bulkText = "";
  let expectedCount = "";

  async function api(path, opts = {}) {
    const r = await fetch(path, {
      headers: { "Content-Type": "application/json", ...(opts.headers || {}) },
      ...opts,
    });
    if (r.status === 401) {
      location.href = "/login";
      throw new Error("未登录");
    }
    const j = await r.json().catch(() => ({}));
    if (!r.ok) {
      const err = new Error(j.error || j.detail || ("HTTP " + r.status));
      err.payload = j;
      throw err;
    }
    return j;
  }

  function setMsg(t) {
    msgEl.textContent = t || "";
  }

  function esc(v) {
    return String(v == null ? "" : v)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  async function refresh() {
    meta = await api("/api/meta");
    document.getElementById("lab-brand").textContent = meta.lab_name || "湖区清单台";
    batches = (await api("/api/batches")).items || [];
    reports = (await api("/api/visibility-reports")).items || [];
    runs = (await api("/api/bulk-runs")).items || [];
  }

  async function openBatch(id) {
    selected = id;
    detail = await api("/api/batches/" + id);
    tab = "detail";
    syncNav();
    render();
  }

  async function openRun(runId) {
    const data = await api("/api/bulk-runs/" + runId);
    activeRun = data.run;
    tab = "runs";
    syncNav();
    render();
  }

  async function doRegister() {
    if (!selected) return;
    setMsg("");
    try {
      await api("/api/batches/" + selected + "/objects", {
        method: "POST",
        body: JSON.stringify({
          object_key: form.object_key,
          content_hash: form.content_hash,
          byte_size: Number(form.byte_size || 0),
        }),
      });
      form = { object_key: "", content_hash: "", byte_size: "100" };
      await openBatch(selected);
      await refresh();
      setMsg("已登记对象");
      render();
    } catch (e) {
      setMsg(e.message);
    }
  }

  function parseBulk(text) {
    const items = [];
    for (const rawLine of String(text).split(/\r?\n/)) {
      const line = rawLine.trim();
      if (!line) continue;
      const parts = line.split(/[,\t]/).map((s) => s.trim());
      if (parts.length < 2) {
        throw new Error("每行需键,指纹[,字节]");
      }
      const item = { object_key: parts[0], content_hash: parts[1] };
      if (parts[2] !== undefined && parts[2] !== "") {
        item.byte_size = Number(parts[2]);
      }
      items.push(item);
    }
    return items;
  }

  async function doBulk() {
    if (!selected) {
      setMsg("请先打开一个批次");
      return;
    }
    setMsg("");
    activeRun = null;
    let items;
    try {
      items = parseBulk(bulkText);
    } catch (e) {
      setMsg(e.message);
      return;
    }
    if (!items.length) {
      setMsg("批量对象不能为空");
      return;
    }
    const expected =
      String(expectedCount).trim() === "" ? null : Number(expectedCount);
    try {
      const r = await api("/api/batches/" + selected + "/objects/bulk", {
        method: "POST",
        body: JSON.stringify({
          items,
          expected_count: expected,
          commit_boundary: meta ? meta.commit_boundary : undefined,
        }),
      });
      activeRun = r.run;
      bulkText = "";
      expectedCount = "";
      await openBatch(selected);
      await refresh();
      setMsg(
        `整批提交 ${r.run.success_count} 枚 · 失败 0 · 未回滚`,
      );
      render();
    } catch (e) {
      const run = e.payload && e.payload.run;
      if (run) activeRun = run;
      await refresh();
      if (selected) {
        detail = await api("/api/batches/" + selected);
      }
      setMsg(e.message);
      render();
    }
  }

  async function doClose() {
    if (!selected) return;
    setMsg("");
    try {
      await api("/api/batches/" + selected + "/close", { method: "POST", body: "{}" });
      await openBatch(selected);
      await refresh();
      setMsg("清单已闭合");
      render();
    } catch (e) {
      setMsg(e.message);
    }
  }

  async function doReport() {
    setMsg("");
    try {
      const r = await api("/api/visibility-reports", {
        method: "POST",
        body: JSON.stringify({ as_of_date: "2026-11-16" }),
      });
      await refresh();
      tab = "reports";
      syncNav();
      setMsg(r.report.summary);
      render();
    } catch (e) {
      setMsg(e.message);
    }
  }

  function syncNav() {
    document.querySelectorAll("nav [data-tab]").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.tab === tab);
    });
  }

  function batchLabel(id) {
    const b = batches.find((x) => x.id === id);
    return b ? `#${b.id} ${b.zone_name}` : `#${id}`;
  }

  function runResultLabel(run) {
    if (!run) return "";
    if (run.ok) return `成功 ${run.success_count} · 失败 0`;
    return `成功 ${run.success_count} · 失败 ${run.failed_count}`;
  }

  function renderBatches() {
    return `
      <h2>落地批次</h2>
      <p class="muted">主链：选批 → 登记对象 → 闭合清单 → 可见性对账 · 提交边界：${esc(
        meta ? meta.commit_boundary : "",
      )}</p>
      <table>
        <thead><tr><th>编号</th><th>湖区</th><th>业务日</th><th>状态</th><th></th></tr></thead>
        <tbody>
          ${batches
            .map(
              (b) => `<tr>
            <td>${b.id}</td>
            <td>${esc(b.zone_name)}<div class="muted">${esc(b.zone_code)}</div></td>
            <td>${esc(b.biz_date)}</td>
            <td><span class="pill">${esc(b.status)}</span></td>
            <td><button type="button" class="secondary" data-open="${b.id}">打开</button></td>
          </tr>`,
            )
            .join("")}
        </tbody>
      </table>`;
  }

  function renderRunCard(run) {
    if (!run) return "";
    const cls = run.ok ? "ok-card" : "fail-card";
    const rollback = run.rolled_back
      ? '<span class="pill danger">已回滚</span>'
      : '<span class="pill">未回滚</span>';
    return `
      <div class="panel card ${cls}" id="run-card">
        <h3>批量结算 #${run.id} · ${esc(batchLabel(run.batch_id))}</h3>
        <p>${esc(run.detail)} ${rollback}</p>
        <p class="muted">声明 ${run.declared_count} 枚 · 实写 ${run.actual_count} 枚 ·
          成功 ${run.success_count} · 失败 ${run.failed_count} ·
          边界 ${esc(run.commit_boundary)}</p>
        <p class="muted">成功键：${
          run.success_keys.length
            ? run.success_keys.map((k) => `<code>${esc(k)}</code>`).join(" ")
            : "无"
        }</p>
        <p class="muted">失败键：${
          run.failed_keys.length
            ? run.failed_keys.map((k) => `<code>${esc(k)}</code>`).join(" ")
            : "无"
        }</p>
      </div>`;
  }

  function renderRunRows(list, withBatch) {
    const cols = withBatch ? 8 : 6;
    if (!list.length) {
      return `<tr><td colspan="${cols}" class="muted">暂无批量写入记录</td></tr>`;
    }
    return list
      .map(
        (r) => `<tr data-run="${r.id}">
        <td>${r.id}</td>
        ${withBatch ? `<td>${esc(batchLabel(r.batch_id))}</td>` : ""}
        <td>${r.declared_count} / ${r.actual_count}</td>
        <td>${r.success_count} / ${r.failed_count}</td>
        <td>${
          r.success_keys.length
            ? r.success_keys.map((k) => `<code>${esc(k)}</code>`).join(" ")
            : "无"
        }</td>
        <td>${r.rolled_back ? '<span class="pill danger">已回滚</span>' : '<span class="pill">未回滚</span>'}</td>
        <td>${r.ok ? '<span class="pill">成功</span>' : '<span class="pill danger">失败</span>'}</td>
        ${withBatch ? `<td class="muted">${esc(r.created_at)}</td>` : ""}
      </tr>`,
      )
      .join("");
  }

  function renderRuns() {
    return `
      <h2>批量写入</h2>
      <p class="muted">一次请求一个结算 · 提交边界：${esc(
        meta ? meta.commit_boundary : "",
      )}</p>
      <table>
        <thead><tr><th>编号</th><th>批次</th><th>声明 / 实写</th><th>成功 / 失败</th><th>成功键</th><th>回滚</th><th>结果</th><th>时间</th></tr></thead>
        <tbody>${renderRunRows(runs, true)}</tbody>
      </table>
      ${activeRun ? renderRunCard(activeRun) : ""}`;
  }

  function renderDetail() {
    if (!detail) {
      return `<h2>批次详情</h2><p class="muted">请先从落地批次打开一条。</p>`;
    }
    const b = detail.batch;
    const objs = detail.objects || [];
    const batchRuns = detail.bulk_runs || [];
    const cardRun =
      activeRun && activeRun.batch_id === b.id
        ? activeRun
        : batchRuns[0] || null;
    return `
      <h2>批次详情 #${b.id}</h2>
      <p>${esc(b.zone_name)} · ${esc(b.biz_date)} · <span class="pill">${esc(b.status)}</span>
        · 对象 ${objs.length} 枚${b.closed_object_count != null ? ` · 固化 ${b.closed_object_count}` : ""}</p>
      <div class="row-actions">
        <button type="button" id="btn-close">闭合清单</button>
        <button type="button" class="secondary" id="btn-report-from-detail">跑可见性对账</button>
      </div>
      <div class="panel">
        <h3>批量写入</h3>
        <label>对象清单（每行：键,指纹,字节）
          <textarea id="f-bulk" rows="5" placeholder="raw/a.parquet,hash-a,100">${esc(
            bulkText,
          )}</textarea>
        </label>
        <label>声明对象数（可选，需与提交行数一致）
          <input id="f-expected" value="${esc(expectedCount)}" placeholder="留空则不校验" />
        </label>
        <button type="button" id="btn-bulk">批量写入</button>
        <p class="muted">边界 ${esc(meta ? meta.commit_boundary : "")}：任一键撞库整批回滚，前序键不保留。</p>
      </div>
      ${renderRunCard(cardRun)}
      <div class="panel">
        <h3>单枚登记</h3>
        <label>对象键<input id="f-key" value="${esc(form.object_key)}" /></label>
        <label>内容指纹<input id="f-hash" value="${esc(form.content_hash)}" /></label>
        <label>字节数<input id="f-size" value="${esc(form.byte_size)}" /></label>
        <button type="button" class="secondary" id="btn-register">写入登记</button>
      </div>
      <h3>对象列表</h3>
      <table>
        <thead><tr><th>键</th><th>指纹</th><th>字节</th><th>状态</th></tr></thead>
        <tbody>
          ${objs
            .map(
              (o) => `<tr>
            <td>${esc(o.object_key)}</td><td>${esc(o.content_hash)}</td><td>${o.byte_size}</td><td>${esc(o.status)}</td>
          </tr>`,
            )
            .join("")}
        </tbody>
      </table>
      <h3>本批批量结算</h3>
      <table>
        <thead><tr><th>编号</th><th>声明 / 实写</th><th>成功 / 失败</th><th>成功键</th><th>回滚</th><th>结果</th></tr></thead>
        <tbody>${renderRunRows(batchRuns)}</tbody>
      </table>`;
  }

  function renderReports() {
    return `
      <h2>可见性对账</h2>
      <button type="button" id="btn-report">生成对账</button>
      <table>
        <thead><tr><th>编号</th><th>截至</th><th>错位</th><th>摘要</th></tr></thead>
        <tbody>
          ${reports
            .map(
              (r) => `<tr>
            <td>${r.id}</td><td>${esc(r.as_of_date)}</td><td>${r.mismatch_count}</td><td>${esc(r.summary)}</td>
          </tr>`,
            )
            .join("")}
        </tbody>
      </table>`;
  }

  function render() {
    if (tab === "batches") view.innerHTML = renderBatches();
    else if (tab === "detail") view.innerHTML = renderDetail();
    else if (tab === "runs") view.innerHTML = renderRuns();
    else view.innerHTML = renderReports();

    view.querySelectorAll("[data-open]").forEach((btn) => {
      btn.onclick = () => openBatch(Number(btn.dataset.open));
    });
    view.querySelectorAll("[data-run]").forEach((row) => {
      row.onclick = () => openRun(Number(row.dataset.run));
    });
    const bulk = document.getElementById("btn-bulk");
    if (bulk) {
      const area = document.getElementById("f-bulk");
      const exp = document.getElementById("f-expected");
      area.oninput = () => (bulkText = area.value);
      exp.oninput = () => (expectedCount = exp.value);
      bulk.onclick = doBulk;
    }
    const reg = document.getElementById("btn-register");
    if (reg) {
      const key = document.getElementById("f-key");
      const hash = document.getElementById("f-hash");
      const size = document.getElementById("f-size");
      key.oninput = () => (form.object_key = key.value);
      hash.oninput = () => (form.content_hash = hash.value);
      size.oninput = () => (form.byte_size = size.value);
      reg.onclick = doRegister;
    }
    const closeBtn = document.getElementById("btn-close");
    if (closeBtn) closeBtn.onclick = doClose;
    const r1 = document.getElementById("btn-report");
    const r2 = document.getElementById("btn-report-from-detail");
    if (r1) r1.onclick = doReport;
    if (r2) r2.onclick = doReport;
  }

  document.querySelectorAll("nav [data-tab]").forEach((btn) => {
    btn.onclick = () => {
      tab = btn.dataset.tab;
      syncNav();
      render();
    };
  });
  document.getElementById("btn-logout").onclick = () => {
    fetch("/api/logout", { method: "POST" }).then(() => (location.href = "/login"));
  };

  refresh()
    .then(render)
    .catch((e) => setMsg(e.message));
})();
