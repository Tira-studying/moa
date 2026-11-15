/**
 * 카드 실적 화면: 카드 상품별 이번 달 인정 실적과 다음 혜택까지 남은 금액을 보여 주고,
 * 카드 추가·수정·삭제, 카드를 알 수 없는 거래 안내, 명세서-카드 연결을 담당합니다.
 * 서버 계산은 app.py의 /api/card-performance와 card_performance.py가 맡습니다.
 * app.js의 $, api, won, escapeHTML, notify를 사용하므로 app.js 다음에 읽습니다.
 */

let cardPerformanceData = null,
  editingCardProduct = null,
  cardPerformanceSequence = 0;

// 1. 조회와 그리기
// ─────────────────────────────────────────────────────────────────
/** 지출 관리의 조회 기준 월로 카드 실적을 읽어 관리 화면을 그립니다. */
async function loadCardPerformance() {
  const sequence = ++cardPerformanceSequence;
  const month = $('finance-month').value || localMonth();
  $('card-performance-state').textContent = '카드 실적을 계산하는 중입니다.';
  try {
    const data = await api(`/api/card-performance?month=${encodeURIComponent(month)}`);
    if (sequence !== cardPerformanceSequence) return;
    cardPerformanceData = data;
    renderCardPerformance(data);
    fillCardProductChoices(data);
    $('card-performance-state').textContent = data.cards.length
      ? `${data.month} 이용분 기준입니다. 이 금액으로 다음 달 혜택 구간이 정해집니다.`
      : '아직 등록한 카드가 없습니다. 아래 ‘내 카드 추가’에서 추천 값을 불러와 등록하세요.';
  } catch (error) {
    if (sequence === cardPerformanceSequence)
      $('card-performance-state').textContent = error.message;
  }
}

/** 혜택 구간별 달성 여부를 작은 표식으로 만듭니다. */
function cardTierTags(tiers) {
  return tiers
    .map(
      (tier) =>
        `<span class="tag ${tier.met ? 'tier-met' : ''}">${escapeHTML(tier.label)} · ${
          tier.met ? '달성' : `${won(tier.remaining)}원 남음`
        }</span>`,
    )
    .join(' ');
}

/** 다음 혜택 구간까지의 진행 막대 너비(%)입니다. 모든 구간을 달성했으면 100입니다. */
function cardProgress(card) {
  if (!card.benefits.length) return 0;
  if (!card.next) return 100;
  return Math.max(0, Math.min(100, (card.spent / card.next.threshold) * 100));
}

function cardHeadline(card) {
  if (!card.benefits.length) return '실적 조건 없음';
  if (!card.next) return '모든 혜택 구간 달성';
  return `${escapeHTML(card.next.label)}까지 ${won(card.next.remaining)}원 남음`;
}

function renderCardPerformance(data) {
  $('card-performance-list').innerHTML = data.cards
    .map((card) => {
      const previous = card.previous_tiers.filter((tier) => tier.met).map((tier) => tier.label);
      return /* HTML */ `<article class="finance-item card-performance-item">
        <div class="card-performance-head">
          <strong>${escapeHTML(card.name)}</strong>
          <span class="tag">${escapeHTML(card.issuer)}</span>
          ${card.last_fours.map((digits) => `<span class="tag">끝자리 ${escapeHTML(digits)}</span>`).join('')}
        </div>
        <p class="card-performance-amount">
          이번 달 인정 실적 <strong>${won(card.spent)}원</strong> · ${card.count}건
        </p>
        ${
          card.benefits.length
            ? `<div class="pattern-track" aria-hidden="true">
                <span class="pattern-fill" style="width:${cardProgress(card)}%"></span>
              </div>`
            : ''
        }
        <p>${cardHeadline(card)}</p>
        ${card.benefits.length ? `<p>${cardTierTags(card.tiers)}</p>` : ''}
        ${
          card.benefits.length
            ? `<p class="help">지난달 실적 ${won(card.previous_spent)}원 → 이번 달 혜택: ${
                previous.length ? previous.map(escapeHTML).join(', ') : '실적 미달'
              }</p>`
            : ''
        }
        ${
          card.excluded_spent
            ? `<p class="help">실적 제외 분야 ${won(card.excluded_spent)}원은 빼고 계산했습니다.</p>`
            : ''
        }
        ${card.note ? `<p class="help">${escapeHTML(card.note)}</p>` : ''}
        <button class="button" type="button" data-card-edit="${card.id}">수정</button
        ><button class="button" type="button" data-card-delete="${card.id}">삭제</button>
      </article>`;
    })
    .join('');
  $('card-performance-unassigned').hidden = !data.unassigned.length;
  $('card-performance-unassigned-list').innerHTML = data.unassigned
    .map(
      (row) =>
        `<p>${escapeHTML(row.issuer)} · ${
          row.last_four ? `끝자리 <strong>${escapeHTML(row.last_four)}</strong>` : '끝자리 없음'
        } · ${row.count}건 · ${won(row.amount)}원</p>`,
    )
    .join('');
  $('card-performance-statements').hidden = !data.statements.length;
  $('card-performance-statement-list').innerHTML = data.statements
    .map((statement) => {
      const options = data.cards
        .filter((card) => card.issuer === statement.card)
        .map(
          (card) =>
            `<option value="${card.id}" ${card.id === statement.card_product_id ? 'selected' : ''}>${escapeHTML(card.name)}</option>`,
        )
        .join('');
      return `<label class="card-statement-link">${escapeHTML(statement.filename)} · ${statement.count}건
        <select data-statement-card="${statement.id}">
          <option value="">연결 안 함 (통합 명세서)</option>${options}
        </select></label>`;
    })
    .join('');
}

/**
 * 사용자가 끈 기능은 탭·대시보드 요약을 숨깁니다. 켜고 끄는 곳은 설정의 ‘화면 기능 켜기 · 끄기’입니다.
 * 보고 있던 ‘카드 실적’ 탭을 끄면 기본 탭(할부 일정)으로 돌아갑니다.
 */
function applyCardPerformanceVisibility(enabled) {
  $('card-performance-enabled').checked = enabled;
  document.querySelector('[data-finance-tab="performance"]').hidden = !enabled;
  if (!enabled) {
    $('dashboard-card-performance').hidden = true;
    if (!document.querySelector('[data-finance-panel="performance"]').hidden)
      showFinanceTab('installments');
  }
}

/** 대시보드의 짧은 카드 실적 요약입니다. 기능을 껐거나 카드를 등록하지 않았으면 영역을 숨깁니다. */
async function loadCardPerformanceSummary() {
  try {
    const data = await api('/api/card-performance');
    applyCardPerformanceVisibility(data.enabled);
    if (!data.enabled) return;
    const cards = data.cards.filter((card) => card.benefits.length);
    $('dashboard-card-performance').hidden = !cards.length;
    $('dashboard-card-performance-list').innerHTML = cards
      .map(
        (card) =>
          /* HTML */ `<div class="card-performance-row">
            <span class="pattern-label">${escapeHTML(card.name)}</span>
            <span class="pattern-track"
              ><span class="pattern-fill" style="width:${cardProgress(card)}%"></span
            ></span>
            <span class="pattern-amount">${cardHeadline(card)}</span>
          </div>`,
      )
      .join('');
  } catch (error) {
    $('dashboard-card-performance').hidden = true;
  }
}

// 2. 카드 추가·수정 입력
// ─────────────────────────────────────────────────────────────────
/** 카드사·실적 제외 분야·추천 값 선택지를 서버 목록으로 채웁니다. 입력 중인 값은 유지합니다. */
function fillCardProductChoices(data) {
  const form = $('card-product-form'),
    issuer = form.elements.issuer.value;
  form.elements.issuer.innerHTML = data.issuers
    .map((name) => `<option value="${escapeHTML(name)}">${escapeHTML(name)}</option>`)
    .join('');
  if (issuer) form.elements.issuer.value = issuer;
  const checked = new Set(
    [...form.querySelectorAll('[name="excluded"]:checked')].map((input) => input.value),
  );
  $('card-product-categories').innerHTML = data.categories
    .map(
      (category) =>
        `<label><input type="checkbox" name="excluded" value="${escapeHTML(category.key)}" ${
          checked.has(category.key) ? 'checked' : ''
        } />${escapeHTML(category.name)}</label>`,
    )
    .join('');
  $('card-product-preset').innerHTML =
    '<option value="">직접 입력</option>' +
    data.presets
      .map(
        (preset, index) =>
          `<option value="${index}">${escapeHTML(preset.issuer)} · ${escapeHTML(preset.name)}</option>`,
      )
      .join('');
}

/** 화면 입력을 카드 상품 값으로 채웁니다. 추천 값과 수정에서 함께 사용합니다. */
function fillCardProductForm(product) {
  const form = $('card-product-form');
  form.elements.issuer.value = product.issuer;
  form.elements.name.value = product.name;
  form.elements.last_fours.value = (product.last_fours || []).join(', ');
  form.elements.benefits.value = product.benefits
    .map((benefit) => `${benefit.label}, ${benefit.threshold}`)
    .join('\n');
  form.elements.note.value = product.note || '';
  const excluded = new Set(product.excluded_categories || []);
  form
    .querySelectorAll('[name="excluded"]')
    .forEach((input) => (input.checked = excluded.has(input.value)));
}

/** "혜택 이름, 금액" 줄을 서버 형식으로 바꿉니다. 금액의 쉼표·원은 지웁니다. */
function parseCardBenefits(text) {
  return text
    .split('\n')
    .map((line) => line.trim())
    .filter(Boolean)
    .map((line) => {
      // 이름은 가장 짧게 잡아 금액의 천 단위 쉼표(300,000)를 구분자로 오해하지 않습니다.
      const match = line.match(/^(.*?)[,，]\s*(\d{1,3}(?:,\d{3})+|\d+)\s*원?$/);
      if (!match) throw new Error(`혜택 줄을 확인해 주세요: ${line}`);
      return { label: match[1].trim(), threshold: Number(match[2].replaceAll(',', '')) };
    });
}

function resetCardProductForm() {
  editingCardProduct = null;
  $('card-product-form').reset();
  $('card-product-error').textContent = '';
  $('card-product-summary').textContent = '내 카드 추가';
  $('card-product-save').textContent = '카드 저장';
}

$('card-product-preset').onchange = (event) => {
  const preset = cardPerformanceData?.presets[event.target.value];
  if (preset) fillCardProductForm({ ...preset, last_fours: [], excluded_categories: [] });
};
$('card-product-reset').onclick = resetCardProductForm;
$('card-product-form').onsubmit = async (event) => {
  event.preventDefault();
  const form = event.currentTarget,
    button = $('card-product-save');
  $('card-product-error').textContent = '';
  button.disabled = true;
  try {
    const payload = {
      issuer: form.elements.issuer.value,
      name: form.elements.name.value,
      last_fours: form.elements.last_fours.value,
      benefits: parseCardBenefits(form.elements.benefits.value),
      excluded_categories: [...form.querySelectorAll('[name="excluded"]:checked')].map(
        (input) => input.value,
      ),
      note: form.elements.note.value,
    };
    await api(
      editingCardProduct
        ? `/api/card-performance/products/${editingCardProduct}`
        : '/api/card-performance/products',
      {
        method: editingCardProduct ? 'PUT' : 'POST',
        revision: cardPerformanceData?._revision,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      },
    );
    notify(editingCardProduct ? '카드를 수정했습니다.' : '카드를 추가했습니다.');
    resetCardProductForm();
    await loadCardPerformance();
  } catch (error) {
    $('card-product-error').textContent = error.message;
  } finally {
    button.disabled = false;
  }
};

// 3. 목록의 수정·삭제·명세서 연결
// ─────────────────────────────────────────────────────────────────
$('card-performance-list').addEventListener('click', async (event) => {
  const edit = event.target.closest('[data-card-edit]'),
    remove = event.target.closest('[data-card-delete]');
  if (edit) {
    const card = cardPerformanceData.cards.find(
      (item) => item.id === Number(edit.dataset.cardEdit),
    );
    editingCardProduct = card.id;
    fillCardProductForm(card);
    $('card-product-summary').textContent = `카드 수정: ${card.name}`;
    $('card-product-save').textContent = '수정 저장';
    $('card-product-editor').open = true;
    $('card-product-editor').scrollIntoView({ behavior: 'smooth', block: 'start' });
    return;
  }
  if (!remove) return;
  const card = cardPerformanceData.cards.find(
    (item) => item.id === Number(remove.dataset.cardDelete),
  );
  if (!confirm(`‘${card.name}’ 카드를 지울까요? 거래는 지워지지 않습니다.`)) return;
  remove.disabled = true;
  try {
    await api(`/api/card-performance/products/${card.id}`, {
      method: 'DELETE',
      revision: cardPerformanceData._revision,
    });
    if (editingCardProduct === card.id) resetCardProductForm();
    notify('카드를 지웠습니다.');
    await loadCardPerformance();
  } catch (error) {
    remove.disabled = false;
    notify(error.message, true);
  }
});
$('card-performance-statement-list').addEventListener('change', async (event) => {
  const select = event.target.closest('[data-statement-card]');
  if (!select) return;
  select.disabled = true;
  try {
    await api(`/api/card-performance/imports/${select.dataset.statementCard}`, {
      method: 'PUT',
      revision: cardPerformanceData._revision,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ product_id: select.value ? Number(select.value) : null }),
    });
    notify('명세서 연결을 바꿨습니다.');
    await loadCardPerformance();
  } catch (error) {
    notify(error.message, true);
    select.disabled = false;
  }
});
$('finance-month').addEventListener('change', () => {
  if (!document.querySelector('[data-finance-panel="performance"]').hidden) loadCardPerformance();
});
$('card-performance-open').onclick = () => {
  navigateTo('planning');
  showFinanceTab('performance');
};
$('card-performance-enabled').onchange = async (event) => {
  const input = event.currentTarget,
    enabled = input.checked;
  input.disabled = true;
  try {
    // 다른 저장으로 개인 자료 버전이 바뀌었을 수 있어 저장 직전에 최신 버전을 읽습니다.
    const latest = await api('/api/features');
    await api('/api/card-performance/settings', {
      method: 'PUT',
      revision: latest._revision,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled }),
    });
    applyCardPerformanceVisibility(enabled);
    if (enabled) loadCardPerformanceSummary();
    notify(enabled ? '카드 실적을 켰습니다. 지출 관리에서 카드를 등록하세요.' : '카드 실적을 껐습니다.');
  } catch (error) {
    input.checked = !enabled;
    notify(error.message, true);
  } finally {
    input.disabled = false;
  }
};
// 탭 표시 여부와 설정 상자를 맞추고, 첫 화면이 대시보드면 요약을 그립니다.
// app.js의 첫 화면 이동이 이 파일보다 먼저 실행되므로 여기서 한 번 읽습니다.
loadCardPerformanceSummary();
