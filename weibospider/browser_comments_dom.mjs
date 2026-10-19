// Read-only DOM extraction for the authorized browser tool's playwright.evaluate.
// This module does not read cookies, browser storage, or make HTTP requests.
export function readVisibleComments() {
  function visible(element) {
    const box = element.getBoundingClientRect();
    return box.width > 0 && box.height > 0 && box.bottom > 0 && box.top < innerHeight;
  }
  function endVisible(scope, label, excludeLayer = false) {
    return !!scope && Array.from(scope.querySelectorAll('*')).some(e =>
      (!excludeLayer || !e.closest('.wbpro-layer')) &&
      !e.closest('.text, article') &&
      e.children.length === 0 && e.textContent.trim() === label && visible(e));
  }
  function textWithEmoji(node) {
    if (node.nodeType === 3) return node.textContent;
    if (node.nodeType === 1 && node.tagName === 'IMG') return node.getAttribute('alt') || '';
    return Array.from(node.childNodes).map(textWithEmoji).join('');
  }
  function record(element, level) {
    const text = element.querySelector(level === 1 ? '.con1 .text' : '.text');
    const user = text?.querySelector('a[href^="/u/"]');
    if (!text || !user) return null;
    const nickname = user.innerText;
    const value = textWithEmoji(text);
    const content = value.slice(value.indexOf(nickname) + nickname.length).trim().replace(/^:/, '');
    const time = element.innerText.match(/\d{2}-\d{1,2}-\d{1,2} \d{1,2}:\d{2}(?: 来自 [^\n]+)?/);
    const like = element.querySelector('button[title="赞"]');
    const replyLink = Array.from(element.querySelectorAll('a')).find(a => /^共\d+条回复/.test(a.innerText));
    return {comment_id: null, comment_level: level,
      user_id: user.getAttribute('href').split('/').pop(), nick_name: nickname,
      content, display_text: element.innerText,
      created_at_display: time ? time[0] : null,
      like_count_display: like?.innerText.trim() || null,
      reply_count_display: replyLink ? Number(replyLink.innerText.match(/\d+/)[0]) : 0,
      content_truncated: value.includes('查看全部'),
      root_comment_id: null, parent_comment_id: null,
      emoji_labels: Array.from(text.querySelectorAll('img[alt]')).map(img => img.getAttribute('alt'))};
  }
  const rootElements = Array.from(document.querySelectorAll('main .item1'));
  const roots = rootElements.map((e, index) => !e.closest('.wbpro-layer') ?
    {...record(e, 1), dom_index: index} : null).filter(e => e?.user_id);
  const modalReplies = Array.from(document.querySelectorAll('main .item2'))
    .filter(e => !e.parentElement.classList.contains('list2'))
    .map(e => record(e, 2)).filter(Boolean);
  const emptyCommentTips = Array.from(document.querySelectorAll('main .woo-tip-text')).filter(e =>
    !e.closest('.text,.wbpro-layer') &&
    /^(?:还没有人评论哦[~～]?快来抢沙发[！!]?|暂无评论|还没有评论)$/.test(e.textContent.trim()));
  const noComments = emptyCommentTips.some(visible);
  const noCommentsRendered = emptyCommentTips.some(e => {
    const box = e.getBoundingClientRect();
    return box.width > 0 && box.height > 0;
  });
  return {comments: roots, replies: modalReplies,
    no_comments_visible: noComments,
    no_comments_rendered: noCommentsRendered,
    all_loaded: document.body.innerText.includes('已加载全部评论') || noCommentsRendered,
    reply_list_says_exhausted: document.body.innerText.includes('没有更多内容了'),
    roots_end_visible: endVisible(document.querySelector('main'), '已加载全部评论', true) || noComments,
    replies_end_visible: endVisible(document.querySelector('.wbpro-layer'), '没有更多内容了'),
    reply_layer_open: !!document.querySelector('.wbpro-layer'),
    post_visible: !!document.querySelector('main article'),
    verification_visible: Array.from(document.querySelectorAll('iframe')).some(e =>
      /captcha|verify/i.test(e.getAttribute('title') || '') && visible(e)) ||
      Array.from(document.querySelectorAll('body *')).some(e => e.children.length === 0 &&
        !e.closest('.text, article') && visible(e) &&
        /访问频次过高|访问异常|安全验证|完成验证|账号异常/.test(e.textContent)),
    login_required: Array.from(document.querySelectorAll('body *')).some(e =>
      e.children.length === 0 && !e.closest('.text, article') && visible(e) &&
      /登录后.*(?:评论|查看)|请先登录/.test(e.textContent))};
}
