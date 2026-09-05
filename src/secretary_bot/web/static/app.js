(() => {
  "use strict";

  const tg = window.Telegram?.WebApp;
  const state = { bootstrap: null, contacts: [], selectedContact: null, analytics: null, activeView: "overview" };
  const titles = {
    overview: "Огляд", schedule: "Розклад", contacts: "Контакти",
    templates: "Шаблони", classifier: "Класифікація", summary: "Самарі",
    analytics: "Аналітика", logs: "Журнал",
  };
  const actions = ["replied", "dry_run", "skipped_schedule", "skipped_excluded", "skipped_owner_replied", "skipped_window_limit", "skipped_kill_switch", "skipped_inactive", "skipped_unsupported_content", "error"];
  const actionLabels = {
    replied: "Відповів", dry_run: "Прев’ю", skipped_schedule: "Поза розкладом",
    skipped_excluded: "Виключено", skipped_owner_replied: "Власник відповів",
    skipped_window_limit: "Ліміт вікна", skipped_kill_switch: "Вимкнено",
    skipped_inactive: "Неактивне", skipped_unsupported_content: "Непідтримуване", error: "Помилка",
  };
  const timezones = ["Europe/Kyiv", "Europe/Prague", "Europe/Warsaw", "Europe/Berlin", "UTC"];
  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

  tg?.ready();
  tg?.expand();
  tg?.setHeaderColor?.("#101925");
  tg?.setBackgroundColor?.("#0d141f");

  function authHeaders() {
    const headers = { "Content-Type": "application/json" };
    if (tg?.initData) headers["X-Telegram-Init-Data"] = tg.initData;
    return headers;
  }

  async function api(path, options = {}) {
    const response = await fetch(path, { credentials: "same-origin", ...options, headers: { ...authHeaders(), ...(options.headers || {}) } });
    if (response.status === 204) return null;
    const payload = await response.json().catch(() => ({}));
    if (!response.ok) {
      const detail = Array.isArray(payload.detail) ? payload.detail.map((item) => item.msg).join(". ") : payload.detail;
      const error = new Error(detail || "Не вдалося виконати дію");
      error.status = response.status;
      throw error;
    }
    return payload;
  }

  function toast(message, error = false) {
    const node = $("#toast");
    node.textContent = message;
    node.classList.toggle("error", error);
    node.classList.add("show");
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => node.classList.remove("show"), 3000);
  }

  function formatDate(value) {
    if (!value) return "—";
    return new Intl.DateTimeFormat("uk-UA", { dateStyle: "short", timeStyle: "short" }).format(new Date(value));
  }

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[char]));
  }

  async function submit(form, callback) {
    const button = $("button[type=submit]", form);
    const original = button.textContent;
    button.disabled = true;
    button.textContent = "Зберігаємо…";
    try {
      const saved = await callback();
      if (saved === false) return;
      toast("Збережено");
      tg?.HapticFeedback?.notificationOccurred?.("success");
    } catch (error) {
      toast(error.message, true);
      tg?.HapticFeedback?.notificationOccurred?.("error");
    } finally {
      button.disabled = false;
      button.textContent = original;
    }
  }

  async function copyText(value) {
    if (navigator.clipboard?.writeText) return navigator.clipboard.writeText(value);
    const field = document.createElement("textarea");
    field.value = value;
    field.setAttribute("readonly", "");
    field.style.position = "fixed";
    field.style.opacity = "0";
    document.body.append(field);
    field.select();
    document.execCommand("copy");
    field.remove();
  }

  function navigate(view) {
    if (!titles[view]) return;
    state.activeView = view;
    $$("[data-view]").forEach((button) => button.classList.toggle("active", button.dataset.view === view));
    $$("[data-view-panel]").forEach((panel) => panel.classList.toggle("active", panel.dataset.viewPanel === view));
    $("#page-title").textContent = titles[view];
    history.replaceState(null, "", `#${view}`);
    $(`[data-view="${view}"]`)?.scrollIntoView({ block: "nearest", inline: "center" });
    if (view === "contacts") loadContacts();
    if (view === "analytics") loadAnalytics();
    if (view === "logs") loadLogs();
  }

  function renderStatus() {
    const { connection, delivery } = state.bootstrap;
    const live = !connection.dry_run && connection.is_active && !connection.kill_switch;
    const rights = connection.rights || {};
    $("#connection-pill").className = `connection-pill ${live ? "live" : connection.is_active ? "" : "off"}`;
    $("#connection-pill span:last-child").textContent = live ? "Live" : connection.dry_run ? "Dry-run" : "Зупинено";
    $("#status-grid").innerHTML = [
      ["Режим", live ? "Live" : connection.dry_run ? "Dry-run" : "Зупинено", live ? "Відповіді активні" : "Перевірте стан"],
      ["Відправник", delivery.sender_identity === "bot" ? "Секретар" : "Власник", delivery.sender_identity === "bot" ? "З видимим підписом" : "Без підпису"],
      ["Затримка", delivery.sender_identity === "bot" ? `${delivery.bot_delay_seconds}–${Math.min(delivery.delay_max_seconds, 60)} с` : `${delivery.delay_min_seconds}–${delivery.delay_max_seconds} с`, "Випадковий інтервал"],
      ["Права", rights.can_reply ? (rights.can_read_messages ? "Відповідь + читання" : "Тільки відповідь") : "Немає відповіді", rights.can_reply ? "Telegram Business" : "Потрібна увага"],
    ].map(([label, value, note], index) => `<article class="status-card ${index === 3 && !rights.can_reply ? "attention" : ""}"><small>${label}</small><strong>${value}</strong><span>${note}</span></article>`).join("");
  }

  function fillDelivery() {
    const form = $("#delivery-form");
    const data = state.bootstrap.delivery;
    $(`input[name=sender_identity][value=${data.sender_identity}]`, form).checked = true;
    ["delay_min_seconds", "delay_max_seconds", "bot_delay_seconds"].forEach((name) => { form.elements[name].value = data[name]; });
    form.elements.mark_read.checked = data.mark_read;
    form.elements.max_auto_replies_per_window.value = data.max_auto_replies_per_window || 0;
    renderDelayRanges();
  }

  function fillEscalation() {
    const form = $("#escalation-form");
    const data = state.bootstrap.escalation;
    form.elements.enabled.checked = data.enabled;
    form.elements.price_amount.value = data.price_amount;
    form.elements.currency.value = data.currency;
    form.elements.offer_text.value = data.offer_text;
    form.elements.confirm_text.value = data.confirm_text;
    form.elements.decline_text.value = data.decline_text;
    $("#escalation-badge").textContent = data.enabled ? "Увімкнено" : "Вимкнено";
    $("#escalation-badge").classList.toggle("neutral", !data.enabled);
  }

  function renderDelayRanges() {
    const form = $("#delivery-form");
    const ownerMin = form.elements.delay_min_seconds.value;
    const botMin = form.elements.bot_delay_seconds.value;
    const maximum = form.elements.delay_max_seconds.value;
    $("#bot-delay-range").textContent = botMin && maximum ? `${botMin}–${maximum} с` : "—";
    $("#owner-delay-range").textContent = ownerMin && maximum ? `${ownerMin}–${maximum} с` : "—";
  }

  function createWindow(container, data = { weekday_mask: 127, time_from: "22:00", time_to: "08:00", is_active: true }) {
    const node = $("#window-template").content.firstElementChild.cloneNode(true);
    $(".weekday-mask", node).value = String(data.weekday_mask);
    $(".time-from", node).value = data.time_from.slice(0, 5);
    $(".time-to", node).value = data.time_to.slice(0, 5);
    $(".is-active", node).checked = data.is_active;
    $(".remove-window", node).addEventListener("click", () => node.remove());
    container.append(node);
  }

  function windowsPayload(container) {
    return $$(".window-row", container).map((row) => ({
      weekday_mask: Number($(".weekday-mask", row).value),
      time_from: $(".time-from", row).value,
      time_to: $(".time-to", row).value,
      is_active: $(".is-active", row).checked,
    }));
  }

  function fillSchedule() {
    const data = state.bootstrap.schedule;
    const select = $("#timezone-select");
    select.innerHTML = [...new Set([...timezones, data.timezone])].map((zone) => `<option value="${escapeHtml(zone)}">${escapeHtml(zone)}</option>`).join("");
    select.value = data.timezone;
    const container = $("#schedule-windows");
    container.innerHTML = "";
    data.windows.forEach((window) => createWindow(container, window));
  }

  function fillTemplates() {
    const form = $("#templates-form");
    form.elements.off_hours_default.value = state.bootstrap.templates.off_hours_default;
    form.elements.money_priority.value = state.bootstrap.templates.money_priority;
  }

  function fillClassifier() {
    const form = $("#classifier-form");
    const data = state.bootstrap.classifier;
    $("#direction-list").innerHTML = data.directions.map((direction) => `
      <article class="direction-card" data-code="${direction.code}">
        <label>Назва<input class="direction-label" maxlength="80" value="${escapeHtml(direction.label)}" required></label>
        <label>Опис<input class="direction-description" maxlength="500" value="${escapeHtml(direction.description)}" required></label>
        <label class="keywords">Ключові слова, через кому<input class="direction-keywords" value="${escapeHtml(direction.keywords.join(", "))}"></label>
        <label class="switch-row"><span><strong>Напрямок активний</strong><small>${direction.code}</small></span><input class="direction-active" type="checkbox" role="switch" ${direction.is_active ? "checked" : ""} ${direction.code === "general" ? "disabled" : ""}></label>
      </article>`).join("");
    form.elements.system_prompt.value = data.system_prompt;
    form.elements.model.value = data.model;
    form.elements.confidence_min.value = data.confidence_min;
  }

  function fillSummary() {
    const form = $("#summary-form");
    const data = state.bootstrap.summary;
    form.elements.summary_time.value = data.summary_time;
    form.elements.summary_channel_id.value = data.summary_channel_id ?? "";
    form.elements.message_retention_enabled.checked = data.message_retention_enabled;
    $("#retention-badge").textContent = data.message_retention_enabled ? "Зашифровано · 48 год" : "Вимкнено";
    $("#retention-badge").classList.toggle("neutral", !data.message_retention_enabled);
    const size = data.retained_bytes < 1024 ? `${data.retained_bytes} Б` : `${(data.retained_bytes / 1024).toFixed(1)} КБ`;
    $("#retention-stats").textContent = data.message_retention_enabled
      ? `Збережено повідомлень: ${data.retained_message_count} · зашифрований обсяг: ${size}${data.next_deletion_at ? ` · найближче видалення ${formatDate(data.next_deletion_at)}` : ""}`
      : "Тексти повідомлень не зберігаються.";
    const connected = Boolean(data.summary_channel_id);
    const channelName = data.summary_channel_title || (connected ? "Підключений Telegram-канал" : "");
    $("#summary-channel-state").textContent = connected
      ? `${channelName} · щоденне самарі надходитиме в канал.`
      : "Канал не підключено — самарі надходитиме в особистий чат.";
    $("#disconnect-summary-channel").classList.toggle("hidden", !connected);
    $("#choose-summary-channel").classList.toggle("hidden", !tg?.requestChat);
    if (!tg?.requestChat) $("#summary-channel-fallback").open = true;
  }

  const wait = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));

  async function pollChannelRequest(requestId) {
    for (let attempt = 0; attempt < 10; attempt += 1) {
      await wait(600 + attempt * 120);
      const result = await api(`/api/v1/summary/channel-request/${requestId}`);
      if (result.status === "connected") {
        state.bootstrap.summary = result.summary;
        fillSummary();
        toast("Канал підключено");
        tg?.HapticFeedback?.notificationOccurred?.("success");
        return;
      }
      if (result.status === "error") throw new Error(result.error || "Не вдалося підключити канал");
    }
    throw new Error("Telegram ще обробляє вибір. Оновіть панель за кілька секунд.");
  }

  async function withBusyButton(button, label, callback) {
    const original = button.textContent;
    button.disabled = true;
    button.textContent = label;
    try { await callback(); }
    catch (error) {
      toast(error.message, true);
      tg?.HapticFeedback?.notificationOccurred?.("error");
    } finally {
      button.disabled = false;
      button.textContent = original;
    }
  }

  async function loadContacts() {
    const search = $("#contact-search").value.trim();
    try {
      const result = await api(`/api/v1/contacts?search=${encodeURIComponent(search)}`);
      state.contacts = result.items;
      renderContacts();
    } catch (error) { toast(error.message, true); }
  }

  function renderContacts() {
    const list = $("#contact-list");
    if (!state.contacts.length) {
      list.innerHTML = '<div class="empty-row">Контакти з’являться після першого вхідного повідомлення.</div>';
      return;
    }
    list.innerHTML = state.contacts.map((contact) => `<button type="button" class="contact-item ${state.selectedContact?.contact_id === contact.contact_id ? "active" : ""}" data-contact-id="${contact.contact_id}"><strong>${escapeHtml(contact.contact_name || `Контакт ${contact.contact_id}`)}</strong><small>${formatDate(contact.last_incoming_at)} · ${contact.auto_reply_count} відповідей · ${contact.paid_escalation_count}/${contact.off_hours_request_count} платних</small></button>`).join("");
    $$(".contact-item", list).forEach((button) => button.addEventListener("click", () => selectContact(Number(button.dataset.contactId))));
  }

  function selectContact(contactId) {
    state.selectedContact = state.contacts.find((contact) => contact.contact_id === contactId);
    if (!state.selectedContact) return;
    renderContacts();
    const form = $("#contact-form");
    form.classList.remove("empty");
    $("#contact-empty").classList.add("hidden");
    $("#contact-fields").classList.remove("hidden");
    $("#contact-title").textContent = state.selectedContact.contact_name || `Контакт ${contactId}`;
    $("#contact-meta").textContent = `ID ${contactId} · останнє повідомлення ${formatDate(state.selectedContact.last_incoming_at)}`;
    $(`input[name=exclusion][value=${state.selectedContact.exclusion}]`, form).checked = true;
    form.elements.exclusion_until.value = state.selectedContact.exclusion_until ? new Date(state.selectedContact.exclusion_until).toISOString().slice(0, 16) : "";
    const windows = $("#contact-windows");
    windows.innerHTML = "";
    state.selectedContact.windows.forEach((window) => createWindow(windows, window));
  }

  async function loadLogs() {
    const form = $("#log-filter");
    const params = new URLSearchParams();
    if (form.elements.contact_id.value) params.set("contact_id", form.elements.contact_id.value);
    if (form.elements.action.value) params.set("action", form.elements.action.value);
    try {
      const result = await api(`/api/v1/logs?${params}`);
      $("#log-rows").innerHTML = result.items.length ? result.items.map((row) => `<tr><td>${formatDate(row.occurred_at)}</td><td>${row.contact_id}</td><td>${escapeHtml(actionLabels[row.action] || row.action)}</td><td>${escapeHtml(row.category || "—")}</td><td>${escapeHtml(row.error_code || row.template_code || "—")}</td></tr>`).join("") : '<tr><td class="empty-row" colspan="5">За вибраними фільтрами записів немає.</td></tr>';
    } catch (error) { toast(error.message, true); }
  }

  function formatAmounts(amounts) {
    const entries = Object.entries(amounts || {});
    return entries.length ? entries.map(([currency, amount]) => `${amount} ${currency}`).join(", ") : "—";
  }

  function formatCategories(categories) {
    const labels = { general: "Загальне", money: "Оплата", unknown: "Без напрямку" };
    const entries = Object.entries(categories || {});
    return entries.length ? entries.map(([code, count]) => `${labels[code] || code}: ${count}`).join(" · ") : "—";
  }

  function renderAnalytics() {
    const data = state.analytics;
    if (!data) return;
    const totals = data.totals;
    const form = $("#analytics-filter");
    form.elements.date_from.value = data.period.date_from;
    form.elements.date_to.value = data.period.date_to;
    $("#analytics-timezone").textContent = `Часовий пояс: ${data.period.timezone}`;
    $("#analytics-totals").innerHTML = [
      ["Контакти", totals.contacts],
      ["Повідомлення", totals.messages],
      ["Звичайні звернення", totals.ordinary_requests],
      ["Платні звернення", totals.paid_requests],
      ["Питання", `${totals.questions_asked} / ${totals.questions_closed}`],
      ["Нараховано", formatAmounts(totals.paid_amounts)],
    ].map(([label, value]) => `<article><small>${label}</small><strong>${escapeHtml(value)}</strong></article>`).join("");
    $("#analytics-rows").innerHTML = data.items.length ? data.items.map((item) => {
      const name = item.contact_name || `Контакт ${item.contact_id}`;
      const username = item.contact_username ? `<small>@${escapeHtml(item.contact_username)}</small>` : "";
      const categories = Object.keys(item.request_categories).length ? item.request_categories : item.categories;
      return `<tr><td><strong>${escapeHtml(name)}</strong>${username}<small>ID ${item.contact_id}</small></td><td>${item.messages}</td><td>${item.message_directions.in || 0} / ${item.message_directions.out || 0}</td><td>${item.ordinary_requests}</td><td>${item.paid_requests}</td><td>${escapeHtml(formatAmounts(item.paid_amounts))}</td><td>${escapeHtml(formatCategories(categories))}</td><td>${item.questions_asked} / ${item.questions_closed}</td></tr>`;
    }).join("") : '<tr><td class="empty-row" colspan="8">За вибраний період контактів немає.</td></tr>';
  }

  async function loadAnalytics() {
    const form = $("#analytics-filter");
    const params = new URLSearchParams();
    if (form.elements.date_from.value) params.set("date_from", form.elements.date_from.value);
    if (form.elements.date_to.value) params.set("date_to", form.elements.date_to.value);
    try {
      state.analytics = await api(`/api/v1/analytics?${params}`);
      renderAnalytics();
      if (!$("#analytics-month").value) $("#analytics-month").value = state.analytics.period.date_to.slice(0, 7);
    } catch (error) { toast(error.message, true); }
  }

  async function downloadMonthlyPdf() {
    const month = $("#analytics-month").value;
    if (!month) throw new Error("Оберіть місяць");
    const response = await fetch(`/api/v1/analytics/monthly.pdf?month=${encodeURIComponent(month)}`, {
      credentials: "same-origin",
      headers: authHeaders(),
    });
    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      throw new Error(payload.detail || "Не вдалося сформувати PDF");
    }
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `personal-secretary-${month}.pdf`;
    document.body.append(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  function bindEvents() {
    $$("[data-view]").forEach((button) => button.addEventListener("click", () => navigate(button.dataset.view)));
    ["delay_min_seconds", "delay_max_seconds", "bot_delay_seconds"].forEach((name) => {
      $("#delivery-form").elements[name].addEventListener("input", renderDelayRanges);
    });
    $("#delivery-form").addEventListener("submit", (event) => { event.preventDefault(); submit(event.currentTarget, async () => {
      const form = event.currentTarget;
      const rawLimit = Number(form.elements.max_auto_replies_per_window.value);
      state.bootstrap.delivery = await api("/api/v1/delivery", { method: "PUT", body: JSON.stringify({ sender_identity: form.elements.sender_identity.value, delay_min_seconds: Number(form.elements.delay_min_seconds.value), delay_max_seconds: Number(form.elements.delay_max_seconds.value), bot_delay_seconds: Number(form.elements.bot_delay_seconds.value), mark_read: form.elements.mark_read.checked, max_auto_replies_per_window: rawLimit || null }) });
      renderStatus();
    }); });
    $("#escalation-form").addEventListener("submit", (event) => { event.preventDefault(); submit(event.currentTarget, async () => {
      const form = event.currentTarget;
      state.bootstrap.escalation = await api("/api/v1/escalation", { method: "PUT", body: JSON.stringify({ enabled: form.elements.enabled.checked, price_amount: form.elements.price_amount.value, currency: form.elements.currency.value.trim().toUpperCase(), offer_text: form.elements.offer_text.value, confirm_text: form.elements.confirm_text.value, decline_text: form.elements.decline_text.value }) });
      fillEscalation();
    }); });
    $("#add-schedule-window").addEventListener("click", () => createWindow($("#schedule-windows")));
    $("#schedule-form").addEventListener("submit", (event) => { event.preventDefault(); submit(event.currentTarget, async () => {
      const windows = windowsPayload($("#schedule-windows"));
      if (!windows.length) throw new Error("Додайте хоча б одне вікно");
      state.bootstrap.schedule = await api("/api/v1/schedule", { method: "PUT", body: JSON.stringify({ timezone: event.currentTarget.elements.timezone.value, windows }) });
    }); });
    let searchTimer;
    $("#contact-search").addEventListener("input", () => { clearTimeout(searchTimer); searchTimer = setTimeout(loadContacts, 250); });
    $("#add-contact-window").addEventListener("click", () => createWindow($("#contact-windows")));
    $("#contact-form").addEventListener("submit", (event) => { event.preventDefault(); if (!state.selectedContact) return; submit(event.currentTarget, async () => {
      const form = event.currentTarget;
      const exclusion = form.elements.exclusion.value;
      const rawUntil = form.elements.exclusion_until.value;
      const saved = await api(`/api/v1/contacts/${state.selectedContact.contact_id}`, { method: "PUT", body: JSON.stringify({ exclusion, exclusion_until: exclusion === "until" && rawUntil ? new Date(rawUntil).toISOString() : null, windows: windowsPayload($("#contact-windows")) }) });
      const index = state.contacts.findIndex((item) => item.contact_id === saved.contact_id);
      state.contacts[index] = saved;
      state.selectedContact = saved;
      selectContact(saved.contact_id);
    }); });
    $("#templates-form").addEventListener("submit", (event) => { event.preventDefault(); submit(event.currentTarget, async () => {
      const form = event.currentTarget;
      state.bootstrap.templates = await api("/api/v1/templates", { method: "PUT", body: JSON.stringify({ off_hours_default: form.elements.off_hours_default.value, money_priority: form.elements.money_priority.value }) });
      fillTemplates();
    }); });
    $("#classifier-form").addEventListener("submit", (event) => { event.preventDefault(); submit(event.currentTarget, async () => {
      const form = event.currentTarget;
      const directions = $$(".direction-card").map((card) => ({ code: card.dataset.code, label: $(".direction-label", card).value, description: $(".direction-description", card).value, keywords: $(".direction-keywords", card).value.split(",").map((item) => item.trim()).filter(Boolean), is_active: card.dataset.code === "general" || $(".direction-active", card).checked }));
      state.bootstrap.classifier = await api("/api/v1/classifier", { method: "PUT", body: JSON.stringify({ directions, system_prompt: form.elements.system_prompt.value, model: form.elements.model.value, confidence_min: form.elements.confidence_min.value }) });
      fillClassifier();
    }); });
    $("#summary-form").addEventListener("submit", (event) => { event.preventDefault(); submit(event.currentTarget, async () => {
      const form = event.currentTarget;
      const enableRetention = form.elements.message_retention_enabled.checked;
      if (enableRetention && !state.bootstrap.summary.message_retention_enabled) {
        const accepted = window.confirm("Увімкнути зашифроване зберігання текстів повідомлень на строк до 48 годин для формування добового самарі?");
        if (!accepted) {
          form.elements.message_retention_enabled.checked = false;
          toast("Зберігання залишилось вимкненим");
          return false;
        }
      }
      try {
        state.bootstrap.summary = await api("/api/v1/summary", { method: "PUT", body: JSON.stringify({ summary_time: form.elements.summary_time.value, summary_channel_id: form.elements.summary_channel_id.value ? Number(form.elements.summary_channel_id.value) : null, message_retention_enabled: enableRetention }) });
      } catch (error) {
        form.elements.message_retention_enabled.checked = state.bootstrap.summary.message_retention_enabled;
        throw error;
      }
      fillSummary();
    }); });
    $("#choose-summary-channel").addEventListener("click", (event) => withBusyButton(event.currentTarget, "Відкриваємо Telegram…", async () => {
      if (!tg?.requestChat) throw new Error("Відкрийте Mini App у Telegram або скористайтеся посиланням нижче");
      const request = await api("/api/v1/summary/channel-request", { method: "POST" });
      const sent = await new Promise((resolve) => tg.requestChat(request.prepared_id, resolve));
      if (!sent) {
        toast("Вибір каналу скасовано");
        return;
      }
      toast("Перевіряємо доступ до каналу…");
      await pollChannelRequest(request.request_id);
    }));
    $("#connect-summary-channel-link").addEventListener("click", (event) => withBusyButton(event.currentTarget, "Перевіряємо…", async () => {
      const input = $("#summary-channel-reference");
      const reference = input.value.trim();
      if (!reference) throw new Error("Вставте посилання на допис або @username каналу");
      state.bootstrap.summary = await api("/api/v1/summary/channel", { method: "POST", body: JSON.stringify({ reference }) });
      input.value = "";
      fillSummary();
      toast("Канал перевірено й підключено");
      tg?.HapticFeedback?.notificationOccurred?.("success");
    }));
    $("#disconnect-summary-channel").addEventListener("click", (event) => withBusyButton(event.currentTarget, "Відключаємо…", async () => {
      const accepted = window.confirm("Відключити канал? Наступні самарі надходитимуть в особистий чат із ботом.");
      if (!accepted) return;
      state.bootstrap.summary = await api("/api/v1/summary/channel", { method: "DELETE" });
      fillSummary();
      toast("Канал відключено");
    }));
    $("#log-filter").elements.action.innerHTML += actions.map((action) => `<option value="${action}">${escapeHtml(actionLabels[action] || action)}</option>`).join("");
    $("#log-filter").addEventListener("submit", (event) => { event.preventDefault(); loadLogs(); });
    $("#analytics-filter").addEventListener("submit", (event) => { event.preventDefault(); loadAnalytics(); });
    $("#download-monthly-pdf").addEventListener("click", (event) => withBusyButton(event.currentTarget, "Формуємо PDF…", async () => {
      await downloadMonthlyPdf();
      toast("PDF-звіт завантажено");
    }));
    $$(".browser-link-action").forEach((button) => button.addEventListener("click", async () => { try { const result = await api("/api/v1/auth/browser-link", { method: "POST" }); await copyText(result.url); toast("Одноразове посилання скопійовано"); } catch (error) { toast(error.message, true); } }));
    $("#logout").addEventListener("click", async () => { await api("/api/v1/auth/logout", { method: "POST" }); location.reload(); });
  }

  async function init() {
    bindEvents();
    try {
      state.bootstrap = await api("/api/v1/bootstrap");
    } catch (error) {
      $("#loading-state").classList.add("hidden");
      $("#auth-state").classList.remove("hidden");
      $("#app").setAttribute("aria-busy", "false");
      return;
    }
    $("#loading-state").classList.add("hidden");
    $("#views").classList.remove("hidden");
    if (!tg?.initData) $("#logout").classList.remove("hidden");
    renderStatus(); fillDelivery(); fillEscalation(); fillSchedule(); fillTemplates(); fillClassifier(); fillSummary();
    const requested = location.hash.slice(1);
    navigate(titles[requested] ? requested : "overview");
    $("#app").setAttribute("aria-busy", "false");
  }

  init();
})();
