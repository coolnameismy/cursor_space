# 周报模板

把尖括号换成实际值。正选 10–15 篇；编号与简介标题序号一致。

```markdown
# AI Agent 周报（<YYYY>-W<ww>）

> 时间范围：<YYYY-MM-DD> — <YYYY-MM-DD>（UTC）  
> 主题：Claude / OpenAI / 领先机构与人物围绕 **Agent** 的理念、产品、研究、协议  
> 收录：优先一手源；同故事不重复占正选  
> ISO 周：<YYYY-Www>

---

## 文件索引

| # | 日期 | 来源类型 | 机构/人物 | 标题 | 链接 |
|---|------|----------|-----------|------|------|
| 01 | YYYY-MM-DD | 产品发布 | Anthropic | 标题 | [链接](https://example.com) |

---

## 逐篇简介

### 01. <一句话标题>
<3–6 句：是什么、关键事实/数字、为何对 agent 重要、已知边界。>

---

## 本周主线（非独立条目）

1. **<主题>**：<一句话判断>
2. **<主题>**：<一句话判断>

---

## 延伸阅读（未计入正选）

| 标题 | 链接 | 备注 |
|------|------|------|
| 标题 | https://… | 窗口外 / 二手 / 补充 |

---

*整理日期：<YYYY-MM-DD>*
```

## 总索引一行

写入 `docs/agent-weekly/README.md` 表格顶部（新周在上）：

```markdown
| <YYYY-Www> | <YYYY-MM-DD> — <YYYY-MM-DD> | <N> 篇 | [周报](./YYYY-Www.md) | <一句话主线> |
```

## `_seen.json` 形状

```json
{
  "updated": "YYYY-MM-DD",
  "items": [
    {
      "url": "https://…",
      "title": "…",
      "week": "YYYY-Www",
      "story": "anthropic-mhs"
    }
  ]
}
```

`story` 用短横线指纹（机构 + 产品/论文关键词），用来合并同一公告的多篇报道。
