#!/usr/bin/env python3
"""Transform compare.html (dev prototype w/ dock) -> index_prod.html (production)."""
s = open('compare.html').read()

# 1) title + meta + favicon
s = s.replace(
 '<title>corlinman · particle film — direction compare</title>',
 '''<title>corlinman — an agent that lives on your own hardware · 自托管智能体平台</title>
<meta name="description" content="A self-hosted intelligent-agent platform — durable memory, real sandboxed tools, seven chat channels, human-in-the-loop approvals. One binary you run, audit, and own. 自托管智能体平台。">
<meta property="og:title" content="corlinman — an agent that lives on your own hardware">
<meta property="og:description" content="Durable memory · sandboxed tools · seven channels · human-in-the-loop. One binary, your hardware.">
<meta property="og:image" content="https://sweetcornna.github.io/corlinman/logo.png">
<meta property="og:type" content="website">
<link rel="icon" type="image/png" href="logo.png">''')

# 2) nav chrome CSS
s = s.replace(
 '.blink{animation:blink 1.1s steps(1) infinite}',
'''.blink{animation:blink 1.1s steps(1) infinite}
.navright{display:flex;align-items:center;gap:18px}
#langtog{font-family:var(--mono);font-size:12px;letter-spacing:.06em;color:#fff;background:transparent;border:1px solid rgba(255,255,255,.45);border-radius:999px;padding:5px 13px;cursor:pointer;transition:opacity .2s}
#langtog:hover{opacity:.55}
.githubIcon{width:32px;height:32px;display:grid;place-items:center;border:1px solid rgba(255,255,255,.45);border-radius:999px;color:#fff;transition:opacity .2s,transform .2s,border-color .2s}
.githubIcon:hover{opacity:.7;transform:translateY(-1px);border-color:rgba(255,255,255,.72)}
.githubIcon svg{width:16px;height:16px;display:block}''')

# 3) nav: add language toggle
s = s.replace(
 '''<nav>
  <div class="brand"><span class="mk"></span> corlinman</div>
  <div class="links" id="navlinks"></div>
</nav>''',
 '''<nav>
  <div class="brand"><span class="mk"></span> corlinman</div>
  <div class="navright">
    <div class="links" id="navlinks"></div>
    <button id="langtog" type="button"></button>
    <a class="githubIcon" href="https://github.com/sweetcornna/corlinman" target="_blank" rel="noopener" aria-label="GitHub" title="GitHub">
      <svg viewBox="0 0 16 16" aria-hidden="true" focusable="false"><path fill="currentColor" d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82A7.59 7.59 0 0 1 8 3.86c.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0 0 16 8c0-4.42-3.58-8-8-8Z"/></svg>
    </a>
  </div>
</nav>''')

# 4) remove the dev dock entirely
a = s.index('<div id="dock">'); b = s.index('<script>', a)
s = s[:a] + s[b:]

# 5) replace dock JS handlers with a single nav language toggle
import re
hs = s.index('function setOn(row,attr,val)')
he = s.index("addEventListener('scroll',onScroll")
s = s[:hs] + "document.getElementById('langtog').addEventListener('click',()=>{LANG=(LANG==='zh'?'en':'zh');localStorage.setItem('corlinman-lang',LANG);applyUiLang();if(active>=0)setFrame(active);});\n" + s[he:]

# 6) applyUiLang: real nav links + langtog label
NAV_OLD = """  document.getElementById('navlinks').innerHTML=u.nav.map(t=>`<a>${t}</a>`).join('');
  document.getElementById('guideText').textContent=u.guide;
  document.getElementById('foot1').textContent=u.f1; document.getElementById('foot2').textContent=u.f2;
}"""
NAV_NEW = """  const HREF=['#','#','wiki.html','https://github.com/sweetcornna/corlinman'];
  document.getElementById('navlinks').innerHTML=u.nav.map((t,i)=>`<a href="${HREF[i]}"${i===3?' target="_blank" rel="noopener"':''}>${t}</a>`).join('');
  document.getElementById('guideText').textContent=u.guide;
  document.getElementById('foot1').textContent=u.f1; document.getElementById('foot2').textContent=u.f2;
  document.getElementById('langtog').textContent=(LANG==='zh'?'EN':'中文');
}"""
assert NAV_OLD in s, "build_prod: applyUiLang block not found (did the source change?)"
s = s.replace(NAV_OLD, NAV_NEW)

# 7) init: drop setOn('langRow'...)
s = s.replace("setOn('langRow','lang',LANG); applyUiLang(); build();", "applyUiLang(); build();")

# 8) mobile perf guard
s = s.replace(
 """  N=Math.round(Math.min(20000,(W*H)/110)*DENS);
  GRIDCELL=Math.max(5,Math.round(W/240));""",
 """  const mob=W<760;
  N=Math.round(Math.min(mob?9000:20000,(W*H)/(mob?150:110))*DENS);
  GRIDCELL=Math.max(5,Math.round(W/(mob?150:240)));""")

open('index_prod.html','w').write(s)
ok = ('id="dock"' not in s and "langtog').textContent" in s and 'palRow' not in s and 'particle film — direction compare' not in s)
assert ok, "build_prod: prod output failed sanity checks (dock removed? langtog label wired? palRow gone?)"
print('built index_prod.html — dock removed + langtog label wired:', ok)
