/**
 * 카드사별 암호 입력 폼 준비: 실제 입력칸과 제출 버튼을 찾아 정상 제출합니다.
 * 암호 해제 함수를 우회 호출하거나 성공 상태를 강제로 만들지 않습니다.
 */

// Use each sender's actual form controls. Never invoke decryption directly.
/** 카드사별 실제 암호 입력칸과 제출 동작을 준비합니다. */
async function prepareStatementControls(page, card) {
  if (!['삼성', '롯데', '현대', '우리'].includes(card)) return null;
  const password = page.locator('#password');
  if ((await password.count()) !== 1) return null;
  if (card === '현대') {
    // Hyundai's placeholder reveals the real password input through changeText.
    const placeholder = page.locator('#decForm input[name="p2_temp"]');
    if (!(await password.isVisible())) {
      if ((await placeholder.count()) !== 1) return null;
      try {
        await placeholder.waitFor({ state: 'visible', timeout: 4000 });
        await placeholder.focus();
      } catch {
        return null;
      }
    }
    const confirm = page
      .locator('#decForm input[type="image"][alt="보안메일 확인"]:visible')
      .first();
    try {
      await password.waitFor({ state: 'visible', timeout: 3000 });
      await confirm.waitFor({ state: 'visible', timeout: 3000 });
    } catch {
      return null;
    }
    return { password, confirm };
  }
  const confirm = page.locator(card === '롯데' ? 'button[onclick*="UserFunc("]' : '#confirm');
  if ((await confirm.count()) !== 1) return null;
  return { password, confirm };
}
module.exports = { prepareStatementControls };
