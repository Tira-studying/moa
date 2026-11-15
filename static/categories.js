/**
 * 계정별 소비 분야 추가·이름 변경·미사용 추가 분야 삭제를 관리합니다.
 * 편집을 열었을 때의 개인 버전을 유지해 다른 탭의 변경을 덮어쓰지 않습니다.
 * 입력값·설정·응답은 브라우저 저장소에 보관하지 않습니다.
 */
let categorySettingsRevision = null;
let categorySettingsItems = [];
let categorySettingsBusy = false;
let categorySettingsSequence = 0;

/** 이름은 이스케이프하고, 분류에 쓰이지 않는 추가 분야에만 삭제 버튼을 제공합니다. */
function renderCategorySettings() {
  $('category-settings-list').innerHTML = categorySettingsItems
    .map((row) => {
      if (row.key === '미분류')
        return '<p class="help">미분류 · 자동 분류를 위한 기본 분야입니다.</p>';
      return `<form class="category-setting-row" data-category-key="${escapeHTML(row.key)}">
        <label>${row.custom ? '내가 추가한 분야' : '기본 분야'}
          <input name="name" value="${escapeHTML(row.name)}" maxlength="24" required aria-label="${escapeHTML(row.name)} 이름" />
        </label>
        <div class="planning-tabs">
          <button class="button" type="submit">이름 저장</button>
          ${row.custom && !row.in_use ? '<button class="button" type="button" data-category-delete>삭제</button>' : ''}
          ${row.custom && row.in_use ? '<small class="help">사용 중</small>' : ''}
        </div>
      </form>`;
    })
    .join('');
}

/** 저장 중의 연속 제출을 막고, 실패하면 입력한 이름을 그대로 남깁니다. */
function setCategorySettingsBusy(busy) {
  categorySettingsBusy = busy;
  $('category-manager')
    .querySelectorAll('button,input')
    .forEach((element) => (element.disabled = busy));
}

/** 응답을 반영하기 전에 대화상자가 열려 있는지 확인하며, 성공 뒤 거래·필터·그래프를 다시 읽습니다. */
async function changeCategorySettings(path, method, name) {
  if (categorySettingsBusy || categorySettingsRevision === null) return;
  const sequence = categorySettingsSequence;
  setCategorySettingsBusy(true);
  $('category-manager-state').textContent = '분야 설정을 저장하고 있습니다.';
  try {
    const data = await api(path, {
      method,
      revision: categorySettingsRevision,
      headers: { 'Content-Type': 'application/json' },
      ...(method === 'DELETE' ? {} : { body: JSON.stringify({ name }) }),
    });
    if (sequence !== categorySettingsSequence || !$('category-manager').open) return;
    categorySettingsItems = data.items;
    categorySettingsRevision = data._revision;
    renderCategorySettings();
    setCategorySettingsBusy(true);
    if (method === 'POST') $('category-new-name').value = '';
    await refresh();
    if (sequence === categorySettingsSequence)
      $('category-manager-state').textContent = '내 소비 분야를 저장했습니다.';
  } catch (error) {
    if (sequence === categorySettingsSequence)
      $('category-manager-state').textContent = error.message;
  } finally {
    if (sequence === categorySettingsSequence) setCategorySettingsBusy(false);
  }
}

$('manage-categories').onclick = async () => {
  const sequence = ++categorySettingsSequence;
  $('category-manager').showModal();
  $('category-manager-state').textContent = '내 소비 분야를 불러오고 있습니다.';
  $('category-settings-list').replaceChildren();
  $('category-new-name').value = '';
  setCategorySettingsBusy(true);
  try {
    const data = await api('/api/categories');
    if (sequence !== categorySettingsSequence || !$('category-manager').open) return;
    categorySettingsItems = data.items;
    categorySettingsRevision = data._revision;
    renderCategorySettings();
    $('category-manager-state').textContent = '분야 이름은 1~24자, 추가 분야는 최대 30개입니다.';
  } catch (error) {
    if (sequence === categorySettingsSequence)
      $('category-manager-state').textContent = error.message;
  } finally {
    if (sequence === categorySettingsSequence) setCategorySettingsBusy(false);
  }
};
$('category-manager-close').onclick = () => $('category-manager').close();
$('category-manager').addEventListener('close', () => {
  categorySettingsSequence++;
  setCategorySettingsBusy(false);
  categorySettingsItems = [];
  categorySettingsRevision = null;
});
$('category-create-form').onsubmit = (event) => {
  event.preventDefault();
  changeCategorySettings('/api/categories', 'POST', $('category-new-name').value);
};
$('category-settings-list').onsubmit = (event) => {
  const form = event.target.closest('[data-category-key]');
  if (!form) return;
  event.preventDefault();
  changeCategorySettings(
    '/api/categories/' + encodeURIComponent(form.dataset.categoryKey),
    'PUT',
    form.elements.name.value,
  );
};
$('category-settings-list').onclick = (event) => {
  const button = event.target.closest('[data-category-delete]');
  if (!button) return;
  const form = button.closest('[data-category-key]');
  if (confirm('사용하지 않는 이 추가 분야를 삭제할까요?'))
    changeCategorySettings(
      '/api/categories/' + encodeURIComponent(form.dataset.categoryKey),
      'DELETE',
    );
};
