"""검증 범위: 삼성 공식 HTTPS 연결의 목적지·크기 제한.

실제 명세서·비밀번호 대신 가상 데이터와 임시 DB를 사용합니다.
브라우저가 필요한 검증은 MOA_BROWSER_TESTS=1인 환경에서 실행합니다.
"""

import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("node"), "Node.js required")
class SamsungPolicyTests(unittest.TestCase):
    def test_only_official_https_urls_and_public_addresses(self):
        script = """
const {allowedUrl,publicAddress}=require('./scripts/samsung-network.cjs');
const good=['https://samsungcard.com/','https://bill.samsungcard.com/a?token=fixture'];
const bad=['http://bill.samsungcard.com/','https://samsungcard.com.evil.test/','https://evilsamsungcard.com/','https://bill.samsungcard.com:444/','https://u:p@bill.samsungcard.com/','https://127.0.0.1/','file:///etc/passwd'];
const privateIPs=['127.0.0.1','10.1.2.3','172.16.0.1','192.168.0.1','169.254.1.2','100.64.0.1','::1','::ffff:127.0.0.1','2001:db8::1','2002:7f00:1::'];
console.log(JSON.stringify({good:good.every(allowedUrl),bad:bad.every(v=>!allowedUrl(v)),private:privateIPs.every(v=>!publicAddress(v)),public:publicAddress('93.184.216.34')}));
"""
        result = subprocess.run(
            ["node", "-e", script], cwd=ROOT, capture_output=True, check=True, text=True
        )
        self.assertEqual(
            json.loads(result.stdout), dict(good=True, bad=True, private=True, public=True)
        )

    def test_private_dns_result_is_rejected_before_transport(self):
        script = """
const {fetchSamsung}=require('./scripts/samsung-network.cjs');
fetchSamsung({url:()=> 'https://bill.samsungcard.com/'},{lookup:async()=>[{address:'192.168.1.1',family:4}]}).then(()=>process.exit(1)).catch(()=>console.log('blocked'));
"""
        result = subprocess.run(
            ["node", "-e", script], cwd=ROOT, capture_output=True, check=True, text=True
        )
        self.assertEqual(result.stdout.strip(), "blocked")

    def test_transport_pins_dns_preserves_post_and_does_not_follow_redirects(self):
        script = """
const {EventEmitter}=require('node:events');
const https=require('node:https');
const zlib=require('node:zlib');
let calls=0,pinned=false,post=false,verified=false;
https.request=(url,options,callback)=>{
 calls++;verified=url.protocol==='https:'&&options.rejectUnauthorized!==false;
 options.lookup(url.hostname,{all:false},(error,address,family)=>pinned=!error&&address==='93.184.216.34'&&family===4);
 const outgoing=new EventEmitter();
 outgoing.end=body=>{
  post=options.method==='POST'&&body.toString()==='fixture-token'&&options.headers.origin==='null'&&!options.headers.referer;
  const response=new EventEmitter();response.statusCode=302;
  response.headers={'location':'https://evil.test/','content-encoding':'gzip','set-cookie':['a=fixture; Secure','b=fixture; Secure']};
  callback(response);
  queueMicrotask(()=>{response.emit('data',zlib.gzipSync('fixture-body'));response.emit('end');});
 };
 outgoing.destroy=error=>outgoing.emit('error',error);
 return outgoing;
};
const {fetchSamsung}=require('./scripts/samsung-network.cjs');
fetchSamsung({url:()=> 'https://bill.samsungcard.com/fixture',method:()=> 'POST',allHeaders:async()=>({origin:'https://statement.invalid',referer:'https://statement.invalid/'}),postDataBuffer:()=>Buffer.from('fixture-token')},{lookup:async()=>[{address:'93.184.216.34',family:4}]}).then(result=>console.log(JSON.stringify({pinned,post,verified,once:calls===1,redirect:result.status===302,decoded:result.body.toString()==='fixture-body',encodingRemoved:!('content-encoding' in result.headers),cookies:result.headers['set-cookie'].split(String.fromCharCode(10)).length===2}))).catch(()=>process.exit(1));
"""
        result = subprocess.run(
            ["node", "-e", script], cwd=ROOT, capture_output=True, check=True, text=True
        )
        self.assertTrue(all(json.loads(result.stdout).values()))
