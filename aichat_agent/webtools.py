"""Internet search, bounded page reading, and a full Playwright browser."""
from __future__ import annotations
import base64
import json
import os
from pathlib import Path
from urllib.parse import urlparse,parse_qs,unquote,urljoin
import xml.etree.ElementTree as ET
from html.parser import HTMLParser

import httpx


def http_url(url):
    p=urlparse(url)
    if p.scheme not in ('http','https') or not p.hostname or p.username or p.password:
        raise ValueError('http_url_required')
    return url


class TextHTML(HTMLParser):
    def __init__(self):
        super().__init__();self.parts=[];self.links=[];self.skip=0;self.current=None
    def handle_starttag(self,tag,attrs):
        a=dict(attrs)
        if tag in ('script','style','noscript'):self.skip+=1
        if tag=='a' and a.get('href'):
            self.current={'url':a['href'],'title':''};self.links.append(self.current)
        if tag in ('p','div','br','h1','h2','h3','li','tr'):self.parts.append('\n')
    def handle_endtag(self,tag):
        if tag in ('script','style','noscript'):self.skip=max(0,self.skip-1)
        if tag=='a':self.current=None
    def handle_data(self,data):
        if not self.skip:
            self.parts.append(data)
            if self.current is not None:self.current['title']+=data


async def fetch(url, max_chars=16000):
    url=http_url(url)
    async with httpx.AsyncClient(timeout=25,follow_redirects=False,trust_env=False,headers={'User-Agent':'Mozilla/5.0 (compatible; SophosChat/0.4)'}) as client:
        for _ in range(6):
            async with client.stream('GET',url) as r:
                if r.is_redirect:
                    url=http_url(urljoin(url,r.headers.get('location','')));continue
                r.raise_for_status();raw=bytearray()
                async for chunk in r.aiter_bytes():
                    raw.extend(chunk)
                    if len(raw)>2*1024*1024:raise ValueError('web_page_too_large')
                text=raw.decode(r.encoding or 'utf-8',errors='replace')
                if 'html' in r.headers.get('content-type',''):
                    parser=TextHTML();parser.feed(text)
                    return {'url':str(r.url),'text':'\n'.join(s.strip() for s in ''.join(parser.parts).splitlines() if s.strip())[:max_chars],
                            'links':[{'title':l['title'][:150],'url':urljoin(str(r.url),l['url'])} for l in parser.links[:60]],'untrusted_content':True}
                return {'url':str(r.url),'text':text[:max_chars],'untrusted_content':True}
    raise ValueError('too_many_redirects')


async def search(query, settings):
    query=query.strip()[:500]
    if not query:raise ValueError('search_query_required')
    provider=settings.get('provider','auto');key=settings.get('api_key','');base=settings.get('url','').rstrip('/')
    async with httpx.AsyncClient(timeout=20,follow_redirects=True,trust_env=False,headers={'User-Agent':'Mozilla/5.0 (compatible; SophosChat/0.4)'}) as c:
        if provider=='brave':
            r=await c.get('https://api.search.brave.com/res/v1/web/search',params={'q':query,'count':6},headers={'X-Subscription-Token':key});r.raise_for_status()
            results=[{'title':x['title'],'url':x['url'],'snippet':x.get('description','')} for x in r.json().get('web',{}).get('results',[])[:6]]
        elif provider=='tavily':
            r=await c.post((base or 'https://api.tavily.com')+'/search',headers={'Authorization':'Bearer '+key},json={'query':query,'max_results':6});r.raise_for_status()
            results=[{'title':x['title'],'url':x['url'],'snippet':x.get('content','')[:1000]} for x in r.json().get('results',[])[:6]]
        elif provider=='searxng':
            r=await c.get(base+'/search',params={'q':query,'format':'json'});r.raise_for_status()
            results=[{'title':x['title'],'url':x['url'],'snippet':x.get('content','')} for x in r.json().get('results',[])[:6]]
        else:
            results=[]
            if provider in ('auto','bing'):
                try:
                    r=await c.get('https://www.bing.com/search',params={'q':query,'format':'rss'});r.raise_for_status()
                    results=[{'title':x.findtext('title'),'url':x.findtext('link'),'snippet':x.findtext('description')} for x in ET.fromstring(r.content).findall('./channel/item')[:6]]
                except (httpx.HTTPError,ET.ParseError):
                    if provider=='bing':raise ValueError('search_provider_unavailable') from None
            if not results and provider in ('auto','duckduckgo'):
                r=await c.get('https://html.duckduckgo.com/html/',params={'q':query});r.raise_for_status()
                parser=TextHTML();parser.feed(r.text)
                for l in parser.links:
                    u=urljoin(str(r.url),l['url']);p=urlparse(u)
                    if 'duckduckgo.com' in (p.hostname or ''):
                        u=parse_qs(p.query).get('uddg',[''])[0]
                    if u.startswith(('http://','https://')) and l['title'].strip() and u not in [x['url'] for x in results]:
                        results.append({'title':l['title'].strip()[:200],'url':u,'snippet':''})
                    if len(results)==6:break
        if not results:raise ValueError('search_no_results_or_provider_challenge')
        return {'query':query,'provider':provider,'results':results,'untrusted_content':True}


class Browser:
    def __init__(self):self.pw=None;self.browser=None;self.context=None;self.page=None
    async def start(self):
        if self.browser:return
        from playwright.async_api import async_playwright
        self.pw=await async_playwright().start()
        options={'headless':True,'chromium_sandbox':True}
        if os.environ.get('AICHAT_CHROMIUM_PATH'):options['executable_path']=os.environ['AICHAT_CHROMIUM_PATH']
        self.browser=await self.pw.chromium.launch(**options)
        state=Path.cwd()/'browser-state.json'
        self.context=await self.browser.new_context(accept_downloads=False,**({'storage_state':str(state)} if state.exists() else {}))
        self.page=await self.context.new_page();self.page.set_default_timeout(10000)
    async def act(self,a):
        await self.start()
        action=a['action'];page=self.page
        if action=='goto':await page.goto(http_url(a['url']),wait_until='domcontentloaded',timeout=25000)
        elif action=='click':await page.locator(a['selector']).click()
        elif action=='fill':await page.locator(a['selector']).fill(a.get('value',''))
        elif action=='press':await page.locator(a.get('selector','body')).press(a.get('value','Enter'))
        elif action=='evaluate':
            result=await page.evaluate(a['script'])
            await self.context.storage_state(path=str(Path.cwd()/'browser-state.json'))
            return {'url':page.url,'result':str(result)[:16000]}
        elif action=='screenshot':
            raw=await page.screenshot(full_page=False)
            return {'_image':base64.b64encode(raw).decode(),'mime':'image/png'}
        elif action=='back':await page.go_back(wait_until='domcontentloaded')
        elif action=='scroll':await page.mouse.wheel(0,900)
        elif action!='inspect':raise ValueError('unknown_browser_action')
        if len(self.context.pages)>1:self.page=self.context.pages[-1];page=self.page
        await self.context.storage_state(path=str(Path.cwd()/'browser-state.json'))
        return {'url':page.url,'title':await page.title(),'text':(await page.locator('body').inner_text())[:14000],
                'links':await page.locator('a[href]').evaluate_all("els => els.slice(0,40).map(e => ({title:e.innerText.slice(0,100),url:e.href}))"),'untrusted_content':True}
    async def close(self):
        if self.browser:await self.browser.close()
        if self.pw:await self.pw.stop()
