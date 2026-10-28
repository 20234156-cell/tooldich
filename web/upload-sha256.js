// SHA-256 fallback for trusted HTTP LAN origins without Web Crypto.
window.uploadSHA256=async function(buffer){
  if(globalThis.crypto?.subtle){const result=await crypto.subtle.digest('SHA-256',buffer);return Array.from(new Uint8Array(result),v=>v.toString(16).padStart(2,'0')).join('');}
  const input=new Uint8Array(buffer),length=input.length;
  const bytes=new Uint8Array(Math.ceil((length+9)/64)*64);bytes.set(input);bytes[length]=128;
  const view=new DataView(bytes.buffer);view.setUint32(bytes.length-8,Math.floor(length/0x20000000));view.setUint32(bytes.length-4,(length*8)>>>0);
  const k=[],h=[];let candidate=2;
  while(k.length<64){let prime=true;for(let d=2;d*d<=candidate;d++)if(candidate%d===0){prime=false;break;}if(prime){if(h.length<8)h.push((Math.sqrt(candidate)%1*0x100000000)>>>0);k.push((Math.cbrt(candidate)%1*0x100000000)>>>0);}candidate++;}
  const r=(x,n)=>(x>>>n)|(x<<(32-n)),w=new Uint32Array(64);
  for(let offset=0;offset<bytes.length;offset+=64){
    for(let i=0;i<16;i++)w[i]=view.getUint32(offset+i*4);
    for(let i=16;i<64;i++){const a=w[i-15],b=w[i-2];w[i]=(w[i-16]+(r(a,7)^r(a,18)^(a>>>3))+w[i-7]+(r(b,17)^r(b,19)^(b>>>10)))>>>0;}
    let [a,b,c,d,e,f,g,j]=h;
    for(let i=0;i<64;i++){const t1=(j+(r(e,6)^r(e,11)^r(e,25))+((e&f)^(~e&g))+k[i]+w[i])>>>0,t2=((r(a,2)^r(a,13)^r(a,22))+((a&b)^(a&c)^(b&c)))>>>0;j=g;g=f;f=e;e=(d+t1)>>>0;d=c;c=b;b=a;a=(t1+t2)>>>0;}
    [a,b,c,d,e,f,g,j].forEach((value,i)=>h[i]=(h[i]+value)>>>0);
  }
  return h.map(v=>v.toString(16).padStart(8,'0')).join('');
};
