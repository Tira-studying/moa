/**
 * 영수증 사진: 거래 상세(transaction-editor)와 직접 입력(manual-entry) 창에서 사진을 붙이고, 크게 보고, 지웁니다.
 * 올리기 전에 긴 변 1600px JPEG로 줄여 용량을 줄이고 사진 속 위치 정보(EXIF)를 없앱니다. 서버: receipts.py
 * 이미 저장된 거래는 사진을 고르는 즉시 올리고, 아직 저장하지 않은 새 직접 입력은 저장한 뒤 올립니다(uploadPending).
 * 사진을 올리거나 지우면 화면 버전이 바뀌므로 창의 버전(revision.set)을 함께 갱신하고, 창을 닫을 때 목록을 다시 읽습니다.
 */
const RECEIPT_MAX = 5,
  RECEIPT_EDGE = 1600;
const receiptImageCache = new Map();

/** 사진을 긴 변 1600px JPEG로 다시 그립니다. 다시 그리면 EXIF(촬영 위치 등)는 남지 않습니다. */
async function shrinkReceipt(file) {
  let bitmap;
  try {
    bitmap = await createImageBitmap(file);
  } catch {
    throw new Error('이 사진은 열 수 없어요. 다른 사진을 골라 주세요.');
  }
  const scale = Math.min(1, RECEIPT_EDGE / Math.max(bitmap.width, bitmap.height));
  const canvas = document.createElement('canvas');
  canvas.width = Math.max(1, Math.round(bitmap.width * scale));
  canvas.height = Math.max(1, Math.round(bitmap.height * scale));
  canvas.getContext('2d').drawImage(bitmap, 0, 0, canvas.width, canvas.height);
  bitmap.close?.();
  const blob = await new Promise((resolve) => canvas.toBlob(resolve, 'image/jpeg', 0.82));
  if (!blob) throw new Error('사진을 줄이지 못했어요. 다른 사진을 골라 주세요.');
  return blob;
}

/** 보안 정책(CSP)이 blob: 주소를 막으므로 data: 주소로 바꿔 보여 줍니다. */
function receiptDataURL(blob) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(reader.result);
    reader.onerror = () => reject(new Error('사진을 읽지 못했어요.'));
    reader.readAsDataURL(blob);
  });
}

/** 저장된 사진을 받아 옵니다. 이미지 응답이라 api() 대신 같은 확인 헤더로 직접 요청합니다.
 * shared=true는 함께 보기에서 상대 사진을 읽는 주소입니다(서버가 지금도 보이는 거래인지 다시 확인). */
function receiptImage(id, shared = false) {
  const key = shared ? `shared:${id}` : id;
  if (!receiptImageCache.has(key)) {
    const loading = fetch(`/api/${shared ? 'shared-receipts' : 'receipts'}/${id}`, {
      headers: {
        'X-Requested-With': 'CardDashboard',
        ...(/^\d+$/.test(document.body.dataset.userId || '')
          ? { 'X-Moa-User': document.body.dataset.userId }
          : {}),
      },
    }).then(async (response) => {
      if (response.status === 401) location.replace('/login');
      if (!response.ok) throw new Error('사진을 불러오지 못했어요.');
      return receiptDataURL(await response.blob());
    });
    receiptImageCache.set(key, loading);
    loading.catch(() => receiptImageCache.delete(key));
  }
  return receiptImageCache.get(key);
}

/** 사진 한 장(주소) 또는 여러 장(주소 목록)을 크게 보여 줍니다. */
function openReceiptViewer(urls) {
  const box = $('receipt-viewer-images');
  box.innerHTML = '';
  for (const url of [].concat(urls)) {
    const img = document.createElement('img');
    img.alt = '영수증 사진';
    img.src = url;
    box.append(img);
  }
  $('receipt-viewer').showModal();
}
$('receipt-viewer-close').onclick = () => $('receipt-viewer').close();
$('receipt-viewer').addEventListener('close', () => ($('receipt-viewer-images').innerHTML = ''));

/** 창 하나의 사진 칸을 맡습니다. revision은 그 창이 저장할 때 쓰는 화면 버전을 읽고 바꾸는 함수입니다. */
function receiptPanel(boxId, dialogId, revision) {
  const box = $(boxId),
    input = box.querySelector('[data-receipt-input]'),
    add = box.querySelector('[data-receipt-add]'),
    list = box.querySelector('[data-receipt-list]'),
    note = box.querySelector('[data-receipt-state]');
  const panel = { transactionId: null, ids: [], pending: [], changed: false, session: 0 };

  function render() {
    const saved = panel.ids.map((id) => ({ id })),
      pending = panel.pending.map((item, index) => ({ index, url: item.url }));
    list.innerHTML = [...saved, ...pending]
      .map((item) => {
        const key = item.id
          ? `data-receipt-id="${item.id}"`
          : `data-receipt-pending="${item.index}"`;
        return `<div class="receipt-thumb" ${key}>
          <button type="button" class="receipt-open" aria-label="영수증 사진 크게 보기"><img alt="영수증 사진" /></button>
          <button type="button" class="receipt-remove" aria-label="사진 지우기">×</button>
        </div>`;
      })
      .join('');
    list.querySelectorAll('.receipt-thumb').forEach((thumb) => {
      const img = thumb.querySelector('img');
      if (thumb.dataset.receiptPending !== undefined)
        img.src = panel.pending[Number(thumb.dataset.receiptPending)].url;
      else
        receiptImage(Number(thumb.dataset.receiptId))
          .then((url) => (img.src = url))
          .catch(() => (img.alt = '불러오기 실패'));
    });
    const count = panel.ids.length + panel.pending.length;
    add.disabled = count >= RECEIPT_MAX;
    add.textContent = count ? '＋ 사진 더 올리기' : '＋ 사진 올리기';
  }

  async function upload(transactionId, blob) {
    const form = new FormData();
    form.append('file', blob, 'receipt.jpg');
    const result = await api(`/api/transactions/${transactionId}/receipts`, {
      method: 'POST',
      revision: revision.get(),
      body: form,
    });
    revision.set(result._revision);
    receiptImageCache.set(result.id, receiptDataURL(blob));
    return result.id;
  }

  add.onclick = () => input.click();
  input.onchange = async () => {
    const files = [...input.files],
      session = panel.session;
    input.value = '';
    add.disabled = true;
    try {
      for (const file of files) {
        if (panel.ids.length + panel.pending.length >= RECEIPT_MAX) {
          note.textContent = `사진은 거래 한 건에 ${RECEIPT_MAX}장까지 붙일 수 있어요.`;
          return;
        }
        note.textContent = '사진을 줄이는 중…';
        const blob = await shrinkReceipt(file);
        if (session !== panel.session) return;
        if (panel.transactionId === null) {
          panel.pending.push({ blob, url: await receiptDataURL(blob) });
          note.textContent = '저장하면 사진도 함께 올라가요.';
        } else {
          note.textContent = '사진을 올리는 중…';
          const id = await upload(panel.transactionId, blob);
          if (session !== panel.session) return;
          panel.ids.push(id);
          panel.changed = true;
          note.textContent = '사진을 저장했어요.';
        }
        render();
      }
    } catch (error) {
      if (session === panel.session) note.textContent = error.message;
    } finally {
      if (session === panel.session) render();
    }
  };

  list.onclick = async (event) => {
    const thumb = event.target.closest('.receipt-thumb');
    if (!thumb) return;
    const pending = thumb.dataset.receiptPending,
      id = Number(thumb.dataset.receiptId);
    if (event.target.closest('.receipt-open')) {
      if (pending !== undefined) openReceiptViewer(panel.pending[Number(pending)].url);
      else receiptImage(id).then(openReceiptViewer, (error) => (note.textContent = error.message));
      return;
    }
    if (!event.target.closest('.receipt-remove')) return;
    if (pending !== undefined) {
      panel.pending.splice(Number(pending), 1);
      return render();
    }
    if (!confirm('이 영수증 사진을 지울까요? 되돌릴 수 없습니다.')) return;
    try {
      const result = await api(`/api/receipts/${id}`, {
        method: 'DELETE',
        revision: revision.get(),
      });
      revision.set(result._revision);
      receiptImageCache.delete(id);
      panel.ids = panel.ids.filter((value) => value !== id);
      panel.changed = true;
      note.textContent = '사진을 지웠어요.';
      render();
    } catch (error) {
      note.textContent = error.message;
    }
  };

  // 저장하지 않고 닫아도 사진 변경은 이미 서버에 있으니 목록·화면 버전을 다시 읽습니다.
  $(dialogId).addEventListener('close', () => {
    panel.session += 1;
    if (panel.changed) {
      panel.changed = false;
      refresh();
    }
  });

  return {
    /** item이 없으면 아직 저장하지 않은 새 거래입니다. */
    open(item = null) {
      panel.session += 1;
      panel.transactionId = item ? item.id : null;
      panel.ids = String(item?.receipt_ids || '')
        .split(',')
        .filter(Boolean)
        .map(Number)
        .sort((a, b) => a - b);
      panel.pending = [];
      panel.changed = false;
      note.textContent = '';
      render();
    },
    /** 새 거래를 저장한 뒤 기다리던 사진을 올립니다. 못 올린 장수를 돌려줍니다. */
    async uploadPending(transactionId) {
      const waiting = panel.pending.splice(0);
      let failed = 0;
      for (const item of waiting) {
        try {
          await upload(transactionId, item.blob);
        } catch {
          failed += 1;
        }
      }
      return failed;
    },
  };
}

const transactionReceipts = receiptPanel('transaction-receipts', 'transaction-editor', {
  get: () => transactionEditRevision,
  set: (value) => (transactionEditRevision = value),
});
const manualReceipts = receiptPanel('manual-receipts', 'manual-entry', {
  get: () => manualRevision,
  set: (value) => (manualRevision = value),
});
