"""Lossless public requirement units; no model-authored replacement task text."""
import re


def prose_parts(text):
    # Explicit requirement lists are independently schedulable even when short.
    # Never split fenced examples; retain every byte and any list introduction.
    if '```' not in text and '~~~' not in text:
        starts=[m.start() for m in re.finditer(r'(?m)^(?:\d+[.)]|[-*+])\s+',text)]
        if len(starts)>1:
            return [text[a:b] for a,b in zip([0,*starts[1:]], [*starts[1:],len(text)])]
    # Keep short paragraphs and fenced code intact. Refine only long prose,
    # outside inline code and parentheses, at sentence/line boundaries.
    if len(text) <= 1000 or '```' in text or '~~~' in text:
        return [text]
    cuts=[];code=False;depth=0
    for i,char in enumerate(text):
        if char=='`':code=not code
        if code:continue
        if char in '([{':depth+=1
        elif char in ')]}':depth=max(0,depth-1)
        if depth or char not in '.\n':continue
        before=text[:i+1].splitlines()[-1] if text[:i+1].splitlines() else ''
        if char=='\n' and (not before.strip() or before.rstrip().endswith(':')):continue
        if char=='.' and re.fullmatch(r'\s*\d+\.',before):continue
        after=re.match(r'\s+([A-Z0-9])',text[i+1:]) if char=='.' else re.match(r'\s*([A-Z0-9])',text[i+1:])
        if after:
            cut=i+1+after.start(1)
            if not cuts or cut>cuts[-1]:cuts.append(cut)
    parts=[];start=0
    for end in [*cuts,len(text)]:
        if text[start:end].strip():parts.append(text[start:end]);start=end
    if start<len(text):parts[-1]+=text[start:]
    assert ''.join(parts)==text
    return parts


def clauses(contract):
    paragraphs=re.findall(r'.+?(?:\n\n|\Z)',contract,re.S)
    if not paragraphs or ''.join(paragraphs)!=contract:
        raise ValueError('Cannot partition public contract')
    result=[]
    for i,paragraph in enumerate(paragraphs,1):
        parent=f'T{i:03d}'
        for j,text in enumerate(prose_parts(paragraph),1):
            result.append({'id':parent if j==1 else f'{parent}_{j:02d}', 'text':text,
                           'parent_id':parent,'prerequisites':[] if j==1 else [parent]})
    for i,unit in enumerate(result[:-1]):
        if re.fullmatch(r'\s*#{1,6}\s+[^\n]+\s*',unit['text']):
            unit['disclose_with']=result[i+1]['id']
    return result


def probe_dependencies(probe, units):
    """Source-bound registered disclosures, independently reviewable semantics.

    Bindings supply exact public excerpts, not model-selected IDs. Thus finer
    units can be scheduled without dropping a real probe disclosure dependency.
    """
    needed=set()
    for excerpt in probe.get('task_disclosure_excerpts',[]):
        matches=[u['id'] for u in units if excerpt in u['text']]
        if len(matches)!=1:raise ValueError('Probe disclosure excerpt must identify one exact public unit: '+repr(excerpt))
        needed.update(matches)
    return sorted(needed)
