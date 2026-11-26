"""Discover challenge cards from the authenticated Arena UI."""
import asyncio
import json
import logging
from pathlib import Path
from urllib.parse import urljoin, urlparse

from .challenge_parser import parse_candidate
from core.scope import is_in_scope, validate_challenge_scope, validate_scenario_inventory

log=logging.getLogger(__name__)
DASHBOARD_URL='https://app.grayswan.ai/dashboard'

class DiscoveryError(RuntimeError): pass

async def _read_text(page,selector,timeout=2500):
    try: return (await page.locator(selector).first.inner_text(timeout=timeout)).strip()
    except Exception: return ''

async def _require_arena_auth(browser,page,cfg):
    """Legacy manual guard kept for callers; discovery now refuses expired sessions."""
    if 'login' in page.url.lower() or not await browser.authenticated():
        print('Arena authentication is required. Log in manually in the headed Chromium window; discovery is paused.')
        input('After Arena login is complete, press Enter here: ')
        await browser.save_auth()
        await page.goto(cfg.challenge_entry_url,wait_until='domcontentloaded',timeout=cfg.api_timeout*1000)
    if 'login' in page.url.lower() or not await browser.authenticated():
        raise DiscoveryError('Manual login did not produce an authenticated Arena session')

async def _dashboard_authenticated(page):
    """Use rendered dashboard content, not cookies, to confirm the saved session."""
    parsed=urlparse(page.url)
    if parsed.hostname!='app.grayswan.ai' or parsed.path!='/dashboard': return False
    return await page.evaluate("""() => {
      const text = document.body?.innerText || '';
      const challengesLink = Array.from(document.querySelectorAll('a')).some(a => {
        const r=a.getBoundingClientRect();
        return r.width>0 && r.height>0 && a.innerText.trim().toUpperCase()==='CHALLENGES'
          && new URL(a.href,location.href).pathname==='/challenges';
      });
      return /\\bwelcome\\s+back\\b/i.test(text) && challengesLink;
    }""")

async def _open_challenges_from_dashboard(page):
    """Follow the rendered Challenges navigation link discovered on the dashboard."""
    links=await page.locator('a').evaluate_all("""elements => elements.map((a,index) => {
      const rect=a.getBoundingClientRect();
      return {index,text:(a.innerText||'').trim(),href:a.getAttribute('href'),
        visible:rect.width>0&&rect.height>0};
    }).filter(a=>a.visible&&a.text.toUpperCase()==='CHALLENGES'&&a.href)""")
    if not links: raise DiscoveryError('Authenticated dashboard did not expose its rendered Challenges navigation link')
    href=links[0]['href']
    target=urljoin(page.url,href)
    parsed=urlparse(target)
    if parsed.hostname!='app.grayswan.ai' or parsed.path!='/challenges':
        raise DiscoveryError(f'Rendered Challenges link left the verified Arena route: {target}')
    await page.locator('a').nth(links[0]['index']).click()
    try: await page.wait_for_url(lambda current:urlparse(current).path=='/challenges',timeout=15000)
    except Exception as exc: raise DiscoveryError(f'Challenges navigation did not reach the linked listing: {type(exc).__name__}: {exc}') from exc
    await page.wait_for_timeout(500)
    return page.url

async def _activate_all_filter(page):
    buttons=page.locator('button')
    for index in range(await buttons.count()):
        button=buttons.nth(index)
        try:
            if not await button.is_visible(): continue
            label=' '.join((await button.inner_text(timeout=1000)).split())
            if not label.upper().startswith('ALL '): continue
            parts=label.split()
            total=int(parts[-1])
            if await button.get_attribute('aria-pressed')!='true':
                await button.click()
                await page.wait_for_timeout(250)
            return total
        except (ValueError,TypeError):
            continue
    raise DiscoveryError('Authenticated challenge listing did not expose a rendered ALL count filter')

async def _listing_snapshot(page):
    cards=await page.locator('main article').evaluate_all("""articles => articles.map((article,index) => {
      const heading=article.querySelector('h3');
      const link=Array.from(article.querySelectorAll('a[href]')).find(a => {
        try { return new URL(a.href,location.href).pathname.includes('/arena/challenge/'); }
        catch (_) { return false; }
      });
      return {index,name:(heading?.innerText||'').trim(),url:link?.href||'',
        href:link?.getAttribute('href')||''};
    })""")
    total=await _activate_all_filter(page)
    return {'cards':cards,'total':total,'page_key':urlparse(page.url)._replace(fragment='').geturl()}

async def _advance_listing(page):
    """Click a rendered pagination/load control, otherwise scroll for lazy cards."""
    controls=page.locator('button,a')
    labels={'next','next page','load more','show more'}
    for index in range(await controls.count()):
        control=controls.nth(index)
        try:
            if not await control.is_visible(): continue
            label=' '.join((await control.inner_text(timeout=700)).split()).lower()
            aria=(await control.get_attribute('aria-label') or '').strip().lower()
            title=(await control.get_attribute('title') or '').strip().lower()
            if not ({label,aria,title} & labels): continue
            if await control.get_attribute('disabled') is not None: continue
            if await control.get_attribute('aria-disabled')=='true': continue
            await control.click(timeout=5000)
            await page.wait_for_timeout(400)
            return True
        except Exception:
            continue
    before=await page.evaluate('({top:window.scrollY,height:document.body.scrollHeight})')
    await page.evaluate('window.scrollTo(0,document.body.scrollHeight)')
    await page.wait_for_timeout(400)
    after=await page.evaluate('({top:window.scrollY,height:document.body.scrollHeight})')
    return after['top']>before['top'] or after['height']>before['height']

async def _collect_incrementally(read_snapshot,advance,max_steps=100):
    """Accumulate pages and lazy batches by stable ID; fail closed on short or looping crawls."""
    found={}; first_page_for_id={}; counted_duplicates=set(); seen_page_signatures=set(); page_keys=set(); previous_signature=None
    duplicate_count=0; expected_total=None
    for _ in range(max_steps):
        snapshot=await read_snapshot()
        cards=snapshot['cards']; total=snapshot['total']; page_key=snapshot['page_key']
        if expected_total is None: expected_total=total
        elif total!=expected_total:
            return {'items':list(found.values()),'complete':False,'total':expected_total,
                    'page_count':len(page_keys),'duplicate_count':duplicate_count,
                    'reason':f'Listing ALL count changed during crawl ({expected_total} to {total})'}
        page_keys.add(page_key)
        signature=(page_key,tuple(sorted(str(card.get('id') or card.get('url') or '') for card in cards)))
        page_ids=set()
        for card in cards:
            cid=card.get('id')
            if not cid: continue
            page_ids.add(cid)
            if cid in found:
                duplicate_key=(cid,page_key)
                if first_page_for_id[cid]!=page_key and duplicate_key not in counted_duplicates:
                    duplicate_count+=1; counted_duplicates.add(duplicate_key)
            else:
                found[cid]=card; first_page_for_id[cid]=page_key
        repeated=signature in seen_page_signatures
        seen_page_signatures.add(signature)
        if len(found)==expected_total:
            if previous_signature==signature:
                moved=await advance()
                if not moved:
                    return {'items':list(found.values()),'complete':True,'total':expected_total,
                            'page_count':len(page_keys),'duplicate_count':duplicate_count,'reason':''}
                previous_signature=signature
                continue
            previous_signature=signature
            await advance()
            continue
        if repeated:
            return {'items':list(found.values()),'complete':False,'total':expected_total,
                    'page_count':len(page_keys),'duplicate_count':duplicate_count,
                    'reason':'A listing page repeated before the advertised ALL count was collected'}
        moved=await advance()
        if not moved:
            return {'items':list(found.values()),'complete':False,'total':expected_total,
                    'page_count':len(page_keys),'duplicate_count':duplicate_count,
                    'reason':f'Listing stopped at {len(found)} of {expected_total} unique challenges'}
        previous_signature=signature
    return {'items':list(found.values()),'complete':False,'total':expected_total,
            'page_count':len(page_keys),'duplicate_count':duplicate_count,
            'reason':f'Listing crawl exceeded {max_steps} snapshots before reaching {expected_total} unique challenges'}

def _candidate_from_card(card,listing_url):
    href=card.get('href') or card.get('url')
    if not href: raise ValueError('challenge card has no rendered challenge link')
    url=urljoin(listing_url,href)
    parsed=urlparse(url)
    if parsed.scheme!='https' or parsed.hostname!='app.grayswan.ai' or not parsed.path.startswith('/arena/challenge/'):
        raise ValueError('challenge card link is outside the rendered Arena challenge route')
    item=parse_candidate(url,card.get('name',''))
    if not card.get('name','').strip(): raise ValueError('challenge card has no rendered h3 name')
    return item

async def _extract_detail(page,item,report):
    await page.goto(item['url'],wait_until='commit',timeout=20000)
    expected_path='/arena/challenge/'+item['slug']
    actual=urlparse(page.url)
    if actual.hostname!='app.grayswan.ai' or not (actual.path==expected_path or actual.path.startswith(expected_path+'/')):
        raise PermissionError(f'detail route redirected to {_safe_location(page.url)}')
    try: await page.locator('h1').first.wait_for(state='visible',timeout=12000)
    except Exception as exc: raise RuntimeError(f'challenge detail heading did not render: {type(exc).__name__}') from exc
    try: await page.locator('#challengeScenarioList [data-behavior-id]').first.wait_for(state='visible',timeout=5000)
    except Exception: await page.wait_for_timeout(700)
    heading=await _read_text(page,'h1')
    if heading: item['name']=heading
    item['detail_url']=page.url
    scenarios=await page.locator('#challengeScenarioList [data-behavior-id]').evaluate_all("""buttons => buttons.map(button => ({
      id:button.getAttribute('data-behavior-id'),name:(button.innerText||'').trim().split('\\n')[0]
    }))""")
    item['scenarios']=scenarios
    item['description']=''
    item['criteria']=''
    item['criteria_scope']='rendered_scenarios'
    try:
        panel=page.locator('#workspaceCriteriaPanel')
        item['scenario_details']=[]
        scenario_buttons=page.locator('#challengeScenarioList [data-behavior-id]')
        for index,scenario in enumerate(scenarios):
            button=scenario_buttons.nth(index)
            # Selecting a rendered scenario loads task metadata; it does not start a session.
            await button.evaluate('(element) => element.click()')
            await page.wait_for_timeout(500)
            detail={'id':scenario.get('id'),'name':scenario.get('name'),'description':'','criteria':''}
            task_buttons=page.locator('#behaviorTaskPanel button')
            open_button=None
            for button_index in range(await task_buttons.count()):
                candidate=task_buttons.nth(button_index)
                try:
                    label=' '.join((await candidate.inner_text(timeout=700)).split()).upper()
                    if 'SEE SUCCESS CRITERIA' in label and await candidate.is_visible():
                        open_button=candidate
                        break
                except Exception:
                    continue
            if open_button is not None:
                await open_button.evaluate('(element) => element.click()')
                await panel.wait_for(state='visible',timeout=5000)
                detail['description']=await panel.evaluate("""panel => {
                  const first=panel.firstElementChild;
                  if (!first) return '';
                  const heading=first.querySelector('h2');
                  const paragraph=first.querySelector('p');
                  return paragraph && paragraph!==heading ? paragraph.innerText.trim() : '';
                }""")
                detail['criteria']=await panel.evaluate("""panel => {
                  return Array.from(panel.querySelectorAll('section')).filter(section =>
                    Array.from(section.querySelectorAll('h2')).some(h =>
                      ['success criteria','criteria'].includes(h.innerText.trim().toLowerCase())))
                    .map(section => section.innerText.trim()).filter(Boolean).join('\\n\\n');
                }""")
                if not detail['criteria']:
                    rail_button=page.locator('#workspaceRailCriteria button').first
                    if await rail_button.count():
                        await rail_button.evaluate('(element) => element.click()')
                        await page.wait_for_timeout(250)
                        detail['criteria']=await panel.evaluate("""panel => {
                          return Array.from(panel.querySelectorAll('section')).filter(section =>
                            Array.from(section.querySelectorAll('h2')).some(h =>
                              ['success criteria','criteria'].includes(h.innerText.trim().toLowerCase())))
                            .map(section => section.innerText.trim()).filter(Boolean).join('\\n\\n');
                        }""")
            else:
                task_panel=page.locator('#behaviorTaskPanel')
                detail['description']=await task_panel.evaluate("""panel => {
                  const content=panel.querySelector('[data-slot="card-content"]');
                  if (!content) return '';
                  const label=Array.from(content.querySelectorAll('p')).find(p =>
                    ['harmful task','benign task description','task description'].includes(p.innerText.trim().toLowerCase()));
                  return label?.nextElementSibling?.innerText.trim() || '';
                }""")
            item['scenario_details'].append(detail)
        descriptions=[f"{entry['name']}: {entry['description']}" for entry in item['scenario_details'] if entry['description']]
        criteria=[f"{entry['name']}:\n{entry['criteria']}" for entry in item['scenario_details'] if entry['criteria']]
        item['description']='\n\n'.join(descriptions)
        item['criteria']='\n\n'.join(criteria)
        item['missing_criteria_scenarios']=[{'id':entry['id'],'name':entry['name']} for entry in item['scenario_details'] if not entry['criteria']]
        if item['missing_criteria_scenarios']:
            report['missing_criteria_scenarios'].extend([{'challenge_id':item['id'],**entry} for entry in item['missing_criteria_scenarios']])
    except Exception as exc:
        item['detail_issue']=f'{type(exc).__name__}: {str(exc)[:200]}'
        log.warning('Could not read rendered criteria id=%s: %s',item['id'],exc)
    if not item['description']:
        report['missing_description'].append({'id':item['id'],'url':item['url']})
    if not item['criteria']:
        report['missing_criteria'].append({'id':item['id'],'url':item['url']})

def _safe_location(url):
    parsed=urlparse(url)
    return f'{parsed.hostname or "unknown"}{parsed.path or "/"}'

async def _write_report(report,db):
    if len(report.get('challenges',[]))!=1:
        raise DiscoveryError('Scoped discovery requires exactly the verified Hazard Hunt November challenge')
    validate_scenario_inventory(report['challenges'][0])
    await db.upsert_inventory(report['challenges'],complete=report['listing_complete'] and not report['malformed'])
    out=Path('data/inventory.json'); out.parent.mkdir(parents=True,exist_ok=True)
    temp=out.with_suffix('.json.tmp'); temp.write_text(json.dumps(report,ensure_ascii=False,indent=2)); temp.replace(out)
    await db.event(None,'discovery_completed',{
        'discovered_count':report['discovered_count'],'expected_count':report['expected_count'],
        'inaccessible_count':len(report['inaccessible']),'malformed_count':len(report['malformed']),
        'missing_criteria_count':len(report['missing_criteria']),'listing_pages':report['listing_pages'],
        'listing_complete':report['listing_complete']})

async def discover(browser,db,cfg):
    """Crawl every challenge card visible in the authenticated Challenges listing."""
    owns_page=not bool(browser.pages)
    page=await browser.new_page() if owns_page else browser.pages[0]
    report={'discovered_count':0,'expected_count':None,'configured_expected_count':cfg.expected_challenges,
            'challenges':[],'inaccessible':[],'malformed':[],'missing_description':[],
            'missing_criteria':[],'missing_criteria_scenarios':[],'listing_pages':0,'listing_url':'','listing_complete':False,
            'duplicates_removed':0,'discrepancy':None,'incomplete_reason':None,
            'listing_card_count':0,'excluded_count':0,'excluded':[],'scope_challenge_id':None}
    try:
        await page.goto(DASHBOARD_URL,wait_until='domcontentloaded',timeout=cfg.api_timeout*1000)
        try: await page.locator('h1').first.wait_for(state='visible',timeout=15000)
        except Exception: pass
        if not await _dashboard_authenticated(page):
            raise DiscoveryError('Saved session did not render the authenticated Arena dashboard; discovery stopped without prompting or changing inventory')
        listing_url=await _open_challenges_from_dashboard(page)
        report['listing_url']=_safe_location(listing_url)
        await page.evaluate('window.scrollTo(0,0)')
        malformed_seen=set()
        async def read_snapshot():
            snapshot=await _listing_snapshot(page)
            report['expected_count']=snapshot['total']
            valid=[]
            for card in snapshot['cards']:
                try: valid.append(_candidate_from_card(card,page.url))
                except Exception as exc:
                    malformed=(card.get('name',''),card.get('href',''),str(exc))
                    if malformed not in malformed_seen:
                        malformed_seen.add(malformed)
                        report['malformed'].append({'page':_safe_location(page.url),'name':malformed[0],
                                                    'href':malformed[1],'reason':malformed[2]})
            snapshot['cards']=valid
            return snapshot
        listing=await _collect_incrementally(read_snapshot,lambda:_advance_listing(page))
        report['listing_pages']=listing['page_count']
        report['duplicates_removed']=listing['duplicate_count']
        report['listing_complete']=listing['complete'] and not report['malformed']
        report['incomplete_reason']=listing['reason'] or (None if report['listing_complete'] else 'One or more rendered challenge cards were malformed')
        candidates=listing['items']
        scoped=[item for item in candidates if is_in_scope(item)]
        report['listing_card_count']=len(candidates)
        report['excluded_count']=len(candidates)-len(scoped)
        report['excluded']=[{'id':item.get('id'),'name':item.get('name'),'url':item.get('url'),
                             'reason':'Outside the exact Hazard Hunt November ID, title, and URL allowlist'}
                            for item in candidates if not is_in_scope(item)]
        if len(scoped)!=1:
            raise DiscoveryError(f'Expected exactly one exact Hazard Hunt November listing card; found {len(scoped)}. No fallback inventory was written.')
        report['challenges']=scoped
        report['discovered_count']=len(scoped)
        report['scope_challenge_id']=scoped[0]['id']
        if report['expected_count']!=cfg.expected_challenges:
            report['discrepancy']=(f'Configured expected_challenges={cfg.expected_challenges}, but the authenticated listing advertises '
                                  f'{report["expected_count"]} challenge cards')
        for item in report['challenges']:
            try: await _extract_detail(page,item,report)
            except Exception as exc:
                report['inaccessible'].append({'id':item['id'],'url':item['url'],
                                               'reason':f'{type(exc).__name__}: {str(exc)[:250]}'})
                report['missing_description'].append({'id':item['id'],'url':item['url']})
                report['missing_criteria'].append({'id':item['id'],'url':item['url']})
                log.warning('Challenge detail unavailable id=%s url=%s error=%s',item['id'],item['url'],exc)
        if report['inaccessible'] or report['malformed'] or report['missing_criteria']:
            raise DiscoveryError('Hazard Hunt November detail or criteria could not be verified; scoped inventory was not updated')
        validate_challenge_scope(report['challenges'][0])
        validate_scenario_inventory(report['challenges'][0])
        await _write_report(report,db)
        return report
    finally:
        if owns_page:
            try:
                if not page.is_closed(): await page.close()
            except Exception: log.exception('Could not close discovery page')
