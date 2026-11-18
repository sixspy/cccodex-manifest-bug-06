(() => {
  const view = document.getElementById("view");
  const msgEl = document.getElementById("msg");
  const decisionEl = document.getElementById("decision");
  let tab = "batches";
  let meta = null;
  let batches = [];
  let selected = null;
  let detail = null;
  let spot = null;
  let queue = [];
  let deadLetters = [];
  let board = [];
  let reports = [];
  let form = { object_key: "", content_hash: "", byte_size: "100" };

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

  function escapeHtml(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function showDecision(html, tone) {
    decisionEl.hidden = false;
    decisionEl.className = "decision " + (tone || "");
    decisionEl.innerHTML = html;
  }

  function clearDecision() {
    decisionEl.hidden = true;
    decisionEl.innerHTML = "";
  }

  async function refresh() {
    meta = await api("/api/meta");
    document.getElementById("lab-brand").textContent = meta.lab_name || "湖区清单台";
    batches = (await api("/api/batches")).items || [];
    queue = (await api("/api/close-queue")).items || [];
    deadLetters = (await api("/api/dead-letters")).items || [];
    board = (await api("/api/close-board")).items || [];
    reports = (await api("/api/visibility-reports")).items || [];
  }

  async function loadSpot(id) {
    spot = await api("/api/batches/" + id + "/close-spot");
  }

  async function openBatch(id) {
    selected = id;
    detail = await api("/api/batches/" + id);
    await loadSpot(id);
    tab = "detail";
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

  async function doClose() {
    if (!selected) return;
    setMsg("");
    clearDecision();
    // 连点同一按钮的本地去抖：一按只发一笔，真正争用由后端裁决。
    const btn = document.getElementById("btn-close");
    if (btn) btn.disabled = true;
    try {
      const r = await api("/api/batches/" + selected + "/close", {
        method: "POST",
        body: "{}",
      });
      await openBatch(selected);
      await refresh();
      showDecision(
        "裁决：先手写入正式闭合 <b>" + escapeHtml(r.voucher_no) + "</b>，固化数 <b>" +
          escapeHtml(r.voucher && r.voucher.object_count) +
          "</b>。后到的同一请求将整笔回滚进死信，不写固化数。",
        "ok"
      );
      setMsg(r.message || "清单已闭合");
      render();
    } catch (e) {
      const p = e.payload || {};
      const dl = p.dead_letter || {};
      if (p.outcome === "rolled_back") {
        showDecision(
          "裁决：本笔整笔回滚，未写固化数、未新增编排闭合记录。<br>" +
            "死信因：<b>" + escapeHtml(dl.reason || e.message) + "</b>" +
            (dl.queue_id ? "（队列号 " + escapeHtml(dl.queue_id) + "）" : "") +
            (p.detail && p.detail.winner_voucher_no
              ? "<br>该批正式闭合归属：<b>" + escapeHtml(p.detail.winner_voucher_no) + "</b>"
              : ""),
          "bad"
        );
      }
      setMsg(e.message);
      if (selected) {
        try {
          await openBatch(selected);
          await refresh();
        } catch (ignored) {
          /* 保持原提示 */
        }
      }
      render();
    } finally {
      const again = document.getElementById("btn-close");
      if (again) again.disabled = false;
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

  function chipTone(tone) {
    return "chip chip-" + (tone || "idle");
  }

  function renderSpotCard() {
    if (!spot) return "";
    const b = spot.batch || {};
    return `
      <div class="spot-card ${escapeHtml(spot.chip_tone || "idle")}">
        <div class="spot-head">
          <span class="${chipTone(spot.chip_tone)}">占用芯片 · ${escapeHtml(spot.chip)}</span>
          <span class="muted">批次 #${escapeHtml(b.id)} · ${escapeHtml(b.zone_name || "")}</span>
        </div>
        <p class="spot-line">${escapeHtml(spot.line)}</p>
        <dl class="spot-grid">
          <div><dt>凭证号</dt><dd>${spot.voucher_no ? escapeHtml(spot.voucher_no) : '<span class="muted">无</span>'}</dd></div>
          <div><dt>固化数</dt><dd>${spot.fixed_object_count == null ? '<span class="muted">未固化</span>' : escapeHtml(spot.fixed_object_count)}</dd></div>
          <div><dt>实到已闭合</dt><dd>${escapeHtml(spot.landed_object_count)}</dd></div>
          <div><dt>编排闭合记录</dt><dd>${escapeHtml(spot.close_record_count)}</dd></div>
          <div><dt>同判定</dt><dd>${spot.same_verdict ? "一致" : '<span class="danger">不一致</span>'}</dd></div>
          <div><dt>死信因</dt><dd>${spot.dead_letter_reason ? '<span class="danger">' + escapeHtml(spot.dead_letter_reason) + "</span>" : '<span class="muted">无</span>'}</dd></div>
        </dl>
        <p class="muted small">已裁决 ${escapeHtml(spot.decided_queue_count)} 笔 · 已回滚 ${escapeHtml(spot.rolled_back_queue_count)} 笔</p>
      </div>`;
  }

  function renderBatches() {
    return `
      <h2>落地批次</h2>
      <p class="muted">主链：选批 → 登记对象 → 闭合清单 → 可见性对账</p>
      <table>
        <thead><tr><th>编号</th><th>湖区</th><th>业务日</th><th>状态</th><th>占用</th><th></th></tr></thead>
        <tbody>
          ${batches
            .map((b) => {
              const row = board.find((x) => x.batch_id === b.id);
              const v = row && row.official_close;
              return `<tr>
            <td>${b.id}</td>
            <td>${escapeHtml(b.zone_name)}<div class="muted">${escapeHtml(b.zone_code)}</div></td>
            <td>${escapeHtml(b.biz_date)}</td>
            <td><span class="pill">${escapeHtml(b.status)}</span></td>
            <td>${
              v
                ? '<span class="chip chip-ok">' + escapeHtml(v.voucher_no) + "</span>"
                : row && row.dead_letter_count
                  ? '<span class="chip chip-bad">已回滚</span>'
                  : '<span class="chip chip-idle">未占用</span>'
            }</td>
            <td><button type="button" class="secondary" data-open="${b.id}">打开</button></td>
          </tr>`;
            })
            .join("")}
        </tbody>
      </table>`;
  }

  function renderDetail() {
    if (!detail) {
      return `<h2>批次详情</h2><p class="muted">请先从落地批次打开一条。</p>`;
    }
    const b = detail.batch;
    const objs = detail.objects || [];
    const bid = b.id;
    const qs = queue.filter((x) => x.batch_id === bid);
    const ds = deadLetters.filter((x) => x.batch_id === bid);
    const closed = b.status === "已闭合";
    return `
      <h2>批次详情 #${b.id}</h2>
      <p>${escapeHtml(b.zone_name)} · ${escapeHtml(b.biz_date)} · <span class="pill">${escapeHtml(b.status)}</span></p>
      ${renderSpotCard()}
      <div class="row-actions">
        <button type="button" id="btn-close" ${closed ? "disabled" : ""}>闭合清单</button>
        <button type="button" class="secondary" id="btn-report-from-detail">跑可见性对账</button>
      </div>
      ${closed ? '<p class="muted small">该批已有正式闭合，重复提交只会整笔回滚进死信、不写固化数。</p>' : ""}
      <div class="panel">
        <h3>登记对象</h3>
        <label>对象键<input id="f-key" value="${escapeHtml(form.object_key)}" /></label>
        <label>内容指纹<input id="f-hash" value="${escapeHtml(form.content_hash)}" /></label>
        <label>字节数<input id="f-size" value="${escapeHtml(form.byte_size)}" /></label>
        <button type="button" id="btn-register" ${closed ? "disabled" : ""}>写入登记</button>
      </div>
      <h3>对象列表</h3>
      <table>
        <thead><tr><th>键</th><th>指纹</th><th>字节</th><th>状态</th></tr></thead>
        <tbody>
          ${objs
            .map(
              (o) => `<tr>
            <td>${escapeHtml(o.object_key)}</td><td>${escapeHtml(o.content_hash)}</td><td>${escapeHtml(o.byte_size)}</td><td>${escapeHtml(o.status)}</td>
          </tr>`
            )
            .join("")}
        </tbody>
      </table>
      <h3 class="attach-title">批次附页 · 闭合请求与死信</h3>
      <table class="attach">
        <thead><tr><th>队列号</th><th>操作人</th><th>状态</th><th>凭证号</th><th>回滚因</th><th>时间</th></tr></thead>
        <tbody>
          ${qs.length
            ? qs
                .map(
                  (q) => `<tr>
            <td>${escapeHtml(q.id)}</td><td>${escapeHtml(q.operator)}</td><td>${chipForState(q.state)}</td>
            <td>${q.voucher_no ? escapeHtml(q.voucher_no) : '<span class="muted">—</span>'}</td>
            <td>${q.reason ? '<span class="danger">' + escapeHtml(q.reason) + "</span>" : '<span class="muted">—</span>'}</td>
            <td class="muted">${escapeHtml(q.created_at)}</td>
          </tr>`
                )
                .join("")
            : '<tr><td colspan="6" class="muted">暂无闭合请求</td></tr>'}
        </tbody>
      </table>
      ${
        ds.length
          ? `<p class="muted small">死信 ${ds.length} 条，最近一次：${escapeHtml(ds[0].reason)}</p>`
          : ""
      }`;
  }

  function chipForState(state) {
    if (state === "已裁决") return '<span class="chip chip-ok">已裁决</span>';
    if (state === "已回滚") return '<span class="chip chip-bad">已回滚</span>';
    return '<span class="chip chip-warn">排队中</span>';
  }

  function renderQueue() {
    return `
      <h2>闭合队列</h2>
      <p class="muted">一次点闭合 = 一次入队；裁决后二选一：已裁决带凭证号，已回滚带死信因。</p>
      <table>
        <thead><tr><th>队列号</th><th>批次</th><th>操作人</th><th>状态</th><th>凭证号</th><th>回滚因</th><th>入队</th><th>裁决</th></tr></thead>
        <tbody>
          ${queue.length
            ? queue
                .map(
                  (q) => `<tr>
            <td>${escapeHtml(q.id)}</td>
            <td>${escapeHtml(q.zone_name)}<div class="muted">#${escapeHtml(q.batch_id)}</div></td>
            <td>${escapeHtml(q.operator)}</td>
            <td>${chipForState(q.state)}</td>
            <td>${q.voucher_no ? escapeHtml(q.voucher_no) : '<span class="muted">—</span>'}</td>
            <td>${q.reason ? '<span class="danger">' + escapeHtml(q.reason) + "</span>" : '<span class="muted">—</span>'}</td>
            <td class="muted">${escapeHtml(q.created_at)}</td>
            <td class="muted">${q.decided_at ? escapeHtml(q.decided_at) : "—"}</td>
          </tr>`
                )
                .join("")
            : '<tr><td colspan="8" class="muted">队列为空</td></tr>'}
        </tbody>
      </table>`;
  }

  function renderBoard() {
    return `
      <h2>编排回看台</h2>
      <p class="muted">每批只见一条正式闭合；后到请求计为回滚，不翻倍编排记录。</p>
      <table>
        <thead><tr><th>批次</th><th>正式闭合凭证</th><th>固化数</th><th>编排记录数</th><th>回滚</th><th>死信</th><th>批次状态</th></tr></thead>
        <tbody>
          ${board.length
            ? board
                .map(
                  (x) => `<tr>
            <td>${escapeHtml(x.zone_name)}<div class="muted">#${escapeHtml(x.batch_id)} · ${escapeHtml(x.biz_date)}</div></td>
            <td>${
              x.official_close
                ? '<span class="chip chip-ok">' + escapeHtml(x.official_close.voucher_no) + "</span>" +
                  '<div class="muted">' + escapeHtml(x.official_close.operator) + " · " + escapeHtml(x.official_close.created_at) + "</div>"
                : '<span class="chip chip-idle">无正式闭合</span>'
            }</td>
            <td>${x.official_close ? escapeHtml(x.official_close.object_count) : '<span class="muted">—</span>'}</td>
            <td>${escapeHtml(x.official_count)}</td>
            <td>${x.rolled_back_count ? '<span class="chip chip-bad">' + escapeHtml(x.rolled_back_count) + "</span>" : '<span class="muted">0</span>'}</td>
            <td>${x.dead_letter_count ? escapeHtml(x.dead_letter_count) : '<span class="muted">0</span>'}</td>
            <td><span class="pill">${escapeHtml(x.status)}</span></td>
          </tr>`
                )
                .join("")
            : '<tr><td colspan="7" class="muted">暂无批次</td></tr>'}
        </tbody>
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
            <td>${escapeHtml(r.id)}</td><td>${escapeHtml(r.as_of_date)}</td><td>${escapeHtml(r.mismatch_count)}</td><td>${escapeHtml(r.summary)}</td>
          </tr>`
            )
            .join("")}
        </tbody>
      </table>`;
  }

  function render() {
    if (tab === "batches") view.innerHTML = renderBatches();
    else if (tab === "detail") view.innerHTML = renderDetail();
    else if (tab === "queue") view.innerHTML = renderQueue();
    else if (tab === "board") view.innerHTML = renderBoard();
    else view.innerHTML = renderReports();

    view.querySelectorAll("[data-open]").forEach((btn) => {
      btn.onclick = () => openBatch(Number(btn.dataset.open));
    });
    const reg = document.getElementById("btn-register");
    if (reg) {
      const key = document.getElementById("f-key");
      const hash = document.getElementById("f-hash");
      const size = document.getElementById("f-size");
      if (key) key.oninput = () => (form.object_key = key.value);
      if (hash) hash.oninput = () => (form.content_hash = hash.value);
      if (size) size.oninput = () => (form.byte_size = size.value);
      reg.onclick = doRegister;
    }
    const closeBtn = document.getElementById("btn-close");
    if (closeBtn) closeBtn.onclick = doClose;
    const r1 = document.getElementById("btn-report");
    const r2 = document.getElementById("btn-report-from-detail");
    if (r1) r1.onclick = doReport;
    if (r2) r2.onclick = doReport;
  }

  function reload() {
    const job = tab === "detail" && selected ? openBatch(selected) : refresh();
    return job.then(render).catch((e) => setMsg(e.message));
  }

  document.querySelectorAll("nav [data-tab]").forEach((btn) => {
    btn.onclick = () => {
      tab = btn.dataset.tab;
      syncNav();
      reload();
    };
  });
  document.getElementById("btn-logout").onclick = () => {
    fetch("/api/logout", { method: "POST" }).then(() => (location.href = "/login"));
  };

  refresh()
    .then(render)
    .catch((e) => setMsg(e.message));
})();
