"""Connect the existing HiVe-Mem videos to the published paper, version 1."""
import re

from .hive_guide import GUIDE

PAPER_KEY = 'arxiv:2611.12872'
PAPER = 'https://arxiv.org/html/2611.12872v1'
FIGURES = frozenset({
    'hivemem.png', 'latency_sensitivity.svg', 'evidence_composition.svg',
    'evidence_selection_by_query_type.svg', 'evidence_composition_gpt.svg',
    'evidence_selection_by_query_type_gpt.svg',
})

# Chapter times refer to the accepted, existing cuts, in seconds.
VIDEOS = (
    ('guided', '教學版', '7:42', '跟著書架案例，把「找到記憶」和「選對證據」拆開來看。', (
        (280, GUIDE['S3.F1']['topics'][0]['question'], 'S3.F1', '圖 1', '案例是教學用的情境。對照作者的方法圖，看模型如何分別選出圖像和文字。'),
        (345, GUIDE['S4.T1']['topics'][0]['question'], 'S4.T1', '表 1', '找到同一模型的 LJ 與 Tok. 兩欄，一起核對品質與用量。'),
    )),
    ('original', '原文版', '8:16', '沿著作者的方法與實驗，核對影片裡的說法。', (
        (270, GUIDE['S4.F2']['topics'][0]['question'], 'S4.F2', '圖 2', '主實驗使用 λ=0。圖 2 另外改變成本權重，觀察證據與品質怎麼變。'),
        (420, GUIDE['S4.F3']['question'], 'S4.F3', '圖 3', '固定取七筆記憶，再比較種子與擴展的配置。5:2 是這次實驗的結果。'),
    )),
)


def video_section(item_id, version_id):
    parts = ['<section class="hive-videos" id="hive-videos" data-position><h2>影片導讀</h2>']
    for index, (slug, title, duration, intro, cues) in enumerate(VIDEOS):
        opened = ' open' if index == 0 else ''
        parts.append(f'<details class="hive-film"{opened}><summary>{title}<span>{duration}</span></summary>'
                     f'<p>{intro}</p><video controls playsinline preload="metadata" aria-label="HiVe-Mem {title}" '
                     f'data-film="{title}" data-slug="{slug}" src="/items/{int(item_id)}/media/{slug}.mp4?version={int(version_id)}">'
                     '請使用支援 MP4 的瀏覽器觀看。</video>'
                     '<ol class="film-chapters" aria-label="影片章節與對應證據">')
        for seconds, caption, anchor, label, note in cues:
            parts.append(f'<li><button type="button" class="film-time" data-seek="{seconds}" '
                         f'aria-label="跳到 {seconds//60} 分 {seconds%60} 秒">{seconds//60}:{seconds%60:02d}</button>'
                         f'<div><strong>{caption}</strong><p>{note}</p>'
                         f'<a href="#kh-visual-{anchor}" data-cue-anchor="{anchor}">對照{label}</a></div></li>')
        parts.append('</ol><button type="button" class="text-action" data-ask-time>從目前播放位置追問</button></details>')
    parts.append('<p class="meta">兩支影片由 NotebookLM 製作。原圖、表格與作者的解釋在下方可逐項核對。</p></section>')
    return ''.join(parts)


def valid_bookmark(anchor, version):
    if version['label'] != 'v1' or version['item_key'] != PAPER_KEY:
        return False
    if anchor == 'hive-videos' or (anchor.startswith('kh-visual-') and anchor[10:] in GUIDE):
        return True
    match = re.fullmatch(r'hive-(guided|original)-t([0-9]{1,3})', anchor)
    return bool(match and int(match[2]) <= {'guided':462, 'original':496}[match[1]])


def local_figure(root, url):
    """Only operator-installed files belonging to this exact arXiv version."""
    if url == PAPER:
        name = 'paper.html'
    elif url.startswith(PAPER + '/') and url[len(PAPER) + 1:] in FIGURES:
        name = url[len(PAPER) + 1:]
    else:
        return None
    try:
        with (root/'figures'/name).open('rb') as stream:
            data = stream.read(2 * 1024 * 1024 + 1)
    except FileNotFoundError:
        return None
    if not data or len(data) > 2 * 1024 * 1024:
        raise ValueError('Invalid installed paper asset size')
    return data
