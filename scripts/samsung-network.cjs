/**
 * 사용자가 허용한 경우에만 삼성카드 공식 HTTPS 요청을 중계합니다.
 * 주소·포트·공개 IP를 확인하고 DNS 결과를 연결에 고정하며 요청 수·시간·크기를 제한합니다.
 * 리디렉션도 새 요청으로 다시 검증하므로 이 검사나 차단 목록을 생략하면 안 됩니다.
 */

// Opt-in transport: HTTPS to Samsung Card only, DNS-pinned to a public address.
const dns = require('node:dns').promises;
const https = require('node:https');
const net = require('node:net');
const zlib = require('node:zlib');
/** 삼성카드 공식 HTTPS 주소와 허용 포트인지 확인합니다. */
function allowedUrl(value) {
  try {
    const u = new URL(value);
    return (
      u.protocol === 'https:' &&
      (!u.port || u.port === '443') &&
      !u.username &&
      !u.password &&
      (u.hostname === 'samsungcard.com' || u.hostname.endsWith('.samsungcard.com'))
    );
  } catch {
    return false;
  }
}
/** 내부망·예약 주소로 연결되지 않도록 실제 연결 IP를 검사합니다. */
function publicAddress(address) {
  if (net.isIP(address) === 4) {
    const [a, b, c] = address.split('.').map(Number);
    return !(
      a === 0 ||
      a === 10 ||
      a === 127 ||
      a >= 224 ||
      (a === 169 && b === 254) ||
      (a === 172 && b >= 16 && b <= 31) ||
      (a === 192 && (b === 168 || b === 0)) ||
      (a === 100 && b >= 64 && b <= 127) ||
      (a === 198 && (b === 18 || b === 19 || (b === 51 && c === 100))) ||
      (a === 203 && b === 0 && c === 113)
    );
  }
  return (
    net.isIP(address) === 6 &&
    /^[23][0-9a-f]{3}:/i.test(address) &&
    !/^2001:(?:db8|0):/i.test(address) &&
    !/^2002:/i.test(address)
  );
}
/** 검증한 공개 IP에만 연결해 한 번의 공식 요청을 전달하고 응답 크기·시간을 제한합니다. */
async function fetchSamsung(request, { lookup = dns.lookup, budget } = {}) {
  const value = request.url();
  if (!allowedUrl(value)) throw new Error('destination');
  const url = new URL(value);
  const addresses = await lookup(url.hostname, { all: true, verbatim: true });
  if (!addresses.length || addresses.some((a) => !publicAddress(a.address)))
    throw new Error('address');
  const chosen = addresses.find((a) => a.family === 4) || addresses[0];
  const headers = { ...(await request.allHeaders()) };
  for (const key of ['host', 'connection', 'accept-encoding', 'content-length'])
    delete headers[key];
  headers['accept-encoding'] = 'identity';
  if (headers.origin === 'https://statement.invalid') headers.origin = 'null';
  if (headers.referer?.startsWith('https://statement.invalid/')) delete headers.referer;
  const body = request.postDataBuffer();
  if (body) headers['content-length'] = String(body.length);
  return new Promise((resolve, reject) => {
    // No automatic redirects: each redirected browser request re-enters policy.
    const outgoing = https.request(
      url,
      {
        method: request.method(),
        headers,
        timeout: 7000,
        agent: false,
        lookup: (_host, options, callback) =>
          options.all ? callback(null, [chosen]) : callback(null, chosen.address, chosen.family),
      },
      (response) => {
        const chunks = [];
        let bytes = 0;
        response.on('data', (chunk) => {
          bytes += chunk.length;
          if (budget) budget.bytes += chunk.length;
          if (bytes > 8 * 1024 * 1024 || (budget && budget.bytes > 32 * 1024 * 1024)) {
            response.destroy();
            outgoing.destroy(new Error('size'));
            return;
          }
          chunks.push(chunk);
        });
        response.on('error', reject);
        response.on('end', () => {
          const resultHeaders = {};
          for (const [key, val] of Object.entries(response.headers)) {
            if (
              !['transfer-encoding', 'content-length', 'connection', 'content-encoding'].includes(
                key,
              ) &&
              val !== undefined
            )
              resultHeaders[key] = Array.isArray(val) ? val.join('\n') : String(val);
          }
          try {
            let result = Buffer.concat(chunks);
            const encoding = response.headers['content-encoding'];
            const options = { maxOutputLength: 8 * 1024 * 1024 };
            if (encoding === 'gzip') result = zlib.gunzipSync(result, options);
            else if (encoding === 'br') result = zlib.brotliDecompressSync(result, options);
            else if (encoding === 'deflate') result = zlib.inflateSync(result, options);
            else if (encoding && encoding !== 'identity') throw new Error('encoding');
            if (budget) {
              budget.decoded = (budget.decoded || 0) + result.length;
              if (budget.decoded > 32 * 1024 * 1024) throw new Error('size');
            }
            resolve({ status: response.statusCode, headers: resultHeaders, body: result });
          } catch {
            reject(new Error('response'));
          }
        });
      },
    );
    outgoing.on('timeout', () => outgoing.destroy(new Error('timeout')));
    outgoing.on('error', reject);
    outgoing.end(body);
  });
}
module.exports = { allowedUrl, publicAddress, fetchSamsung };
