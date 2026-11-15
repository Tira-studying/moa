/**
 * 사용 내역 '함께 보기': 나와 함께 보기를 켠 구성원의 한 달 내역을 날짜·승인 시각순으로 합쳐 읽기 전용으로 보여 줍니다.
 * 서버(shared_spending.py)가 허용 목록으로 만든 필드만 쓰며, 이 파일에는 저장·수정 요청을 넣지 않습니다.
 */
let activityScope = 'mine',
  sharedActivitySequence = 0;

const sharedPaymentNames = { card: '카드', bank: '계좌', manual: '직접 입력' };

/** "우리 ·1234"처럼 카드사와 끝 4자리를 함께 보여 줍니다. 계좌·직접 입력은 결제 방식을 덧붙입니다. */
function sharedCardLabel(row) {
  if (row.payment_kind !== 'card')
    return `${row.card} (${sharedPaymentNames[row.payment_kind] || ''})`;
  return row.last_four ? `${row.card} ·${row.last_four}` : row.card;
}

function sharedActivityRow(row) {
  const refund = row.amount < 0;
  const date = row.used_on.slice(5).replace('-', '.');
  return `<div class="shared-activity-row${refund ? ' refund' : ''}${row.mine ? ' mine' : ''}">
    <span class="shared-activity-date">${escapeHTML(date)}${
      row.used_time ? `<small>${escapeHTML(row.used_time)}</small>` : ''
    }</span>
    <span class="shared-activity-main"><strong>${escapeHTML(row.merchant)}</strong>
      <small><span class="shared-who">${escapeHTML(row.who)}</span> ${escapeHTML(row.category)} · ${escapeHTML(
        sharedCardLabel(row),
      )}${row.hidden ? ' · 상대에게 숨김' : ''}</small>${
        row.receipts?.length
          ? `<button type="button" class="button shared-receipts" data-shared-receipts="${row.receipts.join(',')}" data-shared-mine="${row.mine ? 1 : 0}">영수증 사진 ${row.receipts.length}장</button>`
          : ''
      }</span>
    <span class="shared-activity-amount">${won(row.amount)}원</span>${
      row.memo ? `<p class="shared-activity-memo">${escapeHTML(row.memo)}</p>` : ''
    }
  </div>`;
}

/** 사람별 합계 칩. 함께 보기를 끈 구성원은 금액 대신 안내를 보여 줍니다. */
function sharedActivityChip(member, mine = false) {
  const name = escapeHTML(member.username) + (mine ? ' (나)' : '');
  if (member.shared === false)
    return `<div class="shared-chip off"><strong>${name}</strong><span>함께 보기 꺼 둠</span></div>`;
  return `<div class="shared-chip${mine ? ' mine' : ''}"><strong>${name}</strong><span>${won(member.total)}원</span><span>${member.count}건</span></div>`;
}

async function loadSharedActivity() {
  const sequence = ++sharedActivitySequence;
  const month = $('shared-activity-month').value || localMonth();
  $('shared-activity-state').textContent = '불러오는 중…';
  try {
    const data = await api(`/api/shared-transactions?month=${encodeURIComponent(month)}`);
    if (sequence !== sharedActivitySequence) return;
    $('shared-activity-sharing').textContent = data.sharing
      ? '내 사용 내역도 상대에게 보여요. 가릴 거래는 거래 상세에서 숨길 수 있어요.'
      : '내 사용 내역은 지금 상대에게 보이지 않아요(계정 · 설정에서 켤 수 있어요).';
    $('shared-activity-state').textContent = data.members.length
      ? ''
      : '함께 쓰는 다른 구성원이 없어요.';
    const total = [data.me, ...data.members.filter((member) => member.shared)].reduce(
      (sum, member) => sum + member.total,
      0,
    );
    const rows = data.timeline.length
      ? data.timeline.map(sharedActivityRow).join('')
      : '<p class="help">이 달에는 사용 내역이 없어요.</p>';
    $('shared-activity-list').innerHTML = `<div class="shared-chips">${sharedActivityChip(
      data.me,
      true,
    )}${data.members.map((member) => sharedActivityChip(member)).join('')}<div class="shared-chip total"><strong>합계</strong><span>${won(total)}원</span></div></div>
      <article class="shared-activity-member">${rows}</article>`;
  } catch (error) {
    if (sequence !== sharedActivitySequence) return;
    $('shared-activity-state').textContent = error.message;
    $('shared-activity-list').innerHTML = '';
  }
}

/** 내 내역 화면의 입력·필터는 그대로 두고, 함께 보기일 때만 가립니다(CSS: #activity[data-scope]). */
function setActivityScope(scope) {
  activityScope = scope;
  $('activity').dataset.scope = scope;
  $('activity-shared').hidden = scope !== 'shared';
  document.querySelectorAll('[data-activity-scope]').forEach((button) => {
    const active = button.dataset.activityScope === scope;
    button.classList.toggle('active', active);
    button.setAttribute('aria-pressed', String(active));
  });
  if (scope === 'shared') {
    if (!$('shared-activity-month').value) $('shared-activity-month').value = localMonth();
    loadSharedActivity();
  }
}

document
  .querySelectorAll('[data-activity-scope]')
  .forEach((button) => (button.onclick = () => setActivityScope(button.dataset.activityScope)));
for (const [id, delta] of [
  ['shared-activity-prev', -1],
  ['shared-activity-next', 1],
])
  $(id).onclick = () => {
    $('shared-activity-month').value = MoaAnalytics.monthOffset(
      $('shared-activity-month').value || localMonth(),
      delta,
    );
    loadSharedActivity();
  };
$('shared-activity-month').onchange = loadSharedActivity;
// 영수증 사진(receipts.js): 내 사진은 내 주소로, 상대 사진은 함께 보기 전용 주소로 읽기만 합니다.
$('shared-activity-list').addEventListener('click', async (event) => {
  const button = event.target.closest('[data-shared-receipts]');
  if (!button) return;
  const shared = button.dataset.sharedMine !== '1';
  button.disabled = true;
  try {
    const urls = await Promise.all(
      button.dataset.sharedReceipts.split(',').map((id) => receiptImage(Number(id), shared)),
    );
    openReceiptViewer(urls);
  } catch (error) {
    $('shared-activity-state').textContent = error.message;
  } finally {
    button.disabled = false;
  }
});
