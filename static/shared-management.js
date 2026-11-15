/**
 * 허용된 관리 정보의 공동 열람 화면입니다. 서버가 허용 목록으로 만든 자료만 사용합니다.
 * 이 파일에는 저장·삭제 요청이나 상대방 원장으로 이동하는 동작을 넣지 않습니다.
 */
let sharedManagementSequence = 0;

/** 기존 개인 입력은 유지하면서 읽기 공유 화면과 전환합니다. */
function showManagementScope(scope, load = true) {
  const shared = scope === 'shared';
  $('personal-management').hidden = shared;
  $('shared-management').hidden = !shared;
  document.querySelectorAll('[data-management-scope]').forEach((button) => {
    const active = button.dataset.managementScope === scope;
    button.classList.toggle('active', active);
    button.setAttribute('aria-pressed', String(active));
  });
  if (!load) return;
  if (shared) loadSharedManagement();
  else {
    loadFinance();
    loadBillingForMonth();
  }
}

/** 표시는 문자열로 이스케이프하고, 긴 가맹점·계정 이름도 화면 밖으로 넘치지 않게 합니다. */
function sharedList(rows, render, empty = '등록된 자료가 없습니다.') {
  return rows.length ? rows.map(render).join('') : `<p class="help">${escapeHTML(empty)}</p>`;
}

function sharedDuplicate(row) {
  return `<div class="shared-reading-row"><strong>${escapeHTML(row.merchant)}</strong><p>${escapeHTML(row.card)} · ${escapeHTML(row.used_on)} · ${won(row.amount)}원${row.candidate_count ? ` · 후보 ${row.candidate_count}건` : ''}</p></div>`;
}

/** 개인 API와 공유 API의 데이터 형태를 섞지 않아 공유 화면에서 수정 버튼이 생기지 않습니다. */
function renderSharedMember(member) {
  const plans = member.installments,
    bills = member.billing;
  const status = { missing: '미확인', received: '명세서 확인', no_bill: '청구 없음' };
  const recurringStatus = {
    candidate: '검토 후보',
    confirmed: '고정 지출로 확인',
    ignored: '후보에서 제외',
  };
  return /* HTML */ `<article class="shared-management-member">
    <h3>${escapeHTML(member.username)} <span class="tag">열람 전용</span></h3>
    <details class="guidance" open>
      <summary>월별 청구액 · 납부 상태 · 기본 결제일</summary>
      <div class="pattern-highlights">
        ${stat('입력 청구액', won(bills.total) + '원')}${stat(
          '납부 전',
          won(bills.unpaid) + '원',
        )}${stat('청구액 미입력', bills.unknown_count + '곳')}
      </div>
      ${sharedList(
        bills.cards,
        (row) =>
          `<div class="shared-reading-row"><strong>${escapeHTML(managementCardName(row))} · ${row.enabled ? '사용 중' : '사용 해제 · 합계 제외'}</strong><p>${escapeHTML(status[row.statement_status] || row.statement_status)} · 청구액 ${row.amount === null ? '미입력' : won(row.amount) + '원'} · ${row.paid ? '납부 완료' : '납부 완료 표시 없음'}</p><p class="help">기본 결제일 ${row.due_day === null ? '미설정' : row.due_day + '일'} · 이번 달 예정일 ${escapeHTML(row.due_on || '미설정')}</p></div>`,
      )}
      <p class="help">
        직접 기록한 청구·납부 현황입니다. 청구액 미입력과 0원은 다르며 실제 출금 여부는 본인이
        확인합니다.
      </p>
    </details>
    <details class="guidance">
      <summary>분류 규칙</summary>
      <p class="help">
        가맹점 공통 직접 분류입니다. 이번 거래만 지정한 분류와 메모는 공개하지 않습니다. 자동 분류
        기준은 모든 계정에 동일하게 적용됩니다.
      </p>
      ${sharedList(
        member.rules,
        (row) =>
          `<div class="shared-reading-row"><strong>${escapeHTML(row.merchant)}</strong><p>${escapeHTML(row.category)}</p></div>`,
      )}
    </details>
    <details class="guidance">
      <summary>할부 일정</summary>
      <div class="pattern-highlights">
        ${stat('남은 원금', won(plans.remaining_principal) + '원')}${stat(
          '기준 월 예상 원금',
          won(plans.this_month) + '원',
        )}${stat('이전 월 미확인 원금', won(plans.past_unconfirmed) + '원')}
      </div>
      <div class="schedule-grid">
        ${plans.schedule
          .map(
            (row) =>
              `<div><span>${escapeHTML(row.month)}</span><strong>${won(row.amount)}원</strong></div>`,
          )
          .join('')}
      </div>
      ${sharedList(
        plans.plans,
        (row) =>
          `<div class="shared-reading-row"><strong>${escapeHTML(row.name)} · ${escapeHTML(row.card)}</strong><p>${row.paid_count}/${row.months}회 완료 · 남은 ${row.remaining_count}회 · ${won(row.remaining_principal)}원</p><p class="help">전체 원금 ${won(row.principal)}원 · 첫 납부 ${escapeHTML(row.first_month)} · 종료 ${escapeHTML(row.end_month)} · 다음 미확인 ${escapeHTML(row.next_month || '완료')}<br>회당 원금 ${won(row.regular_amount)}원 · 마지막 ${won(row.final_amount)}원 · 수수료 별도</p></div>`,
      )}
    </details>
    <details class="guidance">
      <summary>중복 검토</summary>
      <p class="help">
        해당 구성원의 전체 기간에서 서로 다른 파일 사이에 찾은 후보입니다. 날짜·가맹점·금액·후보
        건수만 공유하고 원본 파일 확인과 제외·복원은 본인이 처리합니다.
      </p>
      <h4>검토할 후보</h4>
      ${sharedList(member.duplicates, sharedDuplicate, '중복 후보가 없습니다.')}
      <h4>집계에서 제외한 내역</h4>
      ${sharedList(member.excluded, sharedDuplicate, '제외한 내역이 없습니다.')}
    </details>
    <details class="guidance">
      <summary>고정 지출</summary>
      ${sharedList(
        member.recurring,
        (row) =>
          `<div class="shared-reading-row"><strong>${escapeHTML(row.merchant)} · ${escapeHTML(row.card)}</strong><p>${escapeHTML(recurringStatus[row.status] || row.status)} · ${row.occurrences}개월 연속 · 최근 ${won(row.amount)}원</p><p class="help">직전 ${won(row.previous_amount)}원 → 최근 ${won(row.amount)}원 · ${row.change === 0 ? '금액 동일' : `${won(Math.abs(row.change))}원 ${row.change > 0 ? '증가' : '감소'}`}<br>마지막 ${escapeHTML(row.last_used_on)} · 다음 예상 ${escapeHTML(row.next_expected)} / ${won(row.amount)}원</p><details class="guidance"><summary>월별 반복 결제 확인</summary>${row.history.map((item) => `<p class="help">${escapeHTML(item.used_on)} · ${won(item.amount)}원</p>`).join('')}</details></div>`,
        '조건에 맞는 반복 결제 후보가 없습니다.',
      )}
    </details>
  </article>`;
}

/** 늦게 도착한 다른 월의 응답은 표시하지 않습니다. 금융 자료를 캐시하지 않습니다. */
async function loadSharedManagement() {
  const sequence = ++sharedManagementSequence;
  const month = $('finance-month').value || localMonth();
  $('finance-month').value = month;
  $('finance-refresh').disabled = true;
  $('shared-management-state').textContent = '함께 볼 관리 정보를 확인하고 있습니다.';
  $('shared-management-list').replaceChildren();
  try {
    const data = await api('/api/shared-management?month=' + encodeURIComponent(month));
    if (sequence !== sharedManagementSequence) return;
    $('shared-management-list').innerHTML = data.members.map(renderSharedMember).join('');
    $('shared-management-state').textContent =
      `${month} · 활성 구성원 ${data.members.length}명 · 함께 열람, 수정은 본인만 가능합니다.`;
  } catch (error) {
    if (sequence === sharedManagementSequence)
      $('shared-management-state').textContent = error.message;
  } finally {
    if (sequence === sharedManagementSequence) $('finance-refresh').disabled = false;
  }
}

document.querySelectorAll('[data-management-scope]').forEach((button) => {
  button.onclick = () => showManagementScope(button.dataset.managementScope);
});
