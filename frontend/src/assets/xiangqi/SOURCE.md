# 棋盘与棋子素材来源

本目录的 `board.svg` 与 `{r,b}{A,B,C,K,N,P,R}.svg` 来自开源项目：

- 仓库：[lengyanyu258/xiangqiboardjs](https://github.com/lengyanyu258/xiangqiboardjs)（MIT License，chessboard.js 的中国象棋分支）
- 上游路径：
  - 棋盘 `docs/img/xiangqiboards/wikimedia/xiangqiboard.svg`
  - 棋子 `docs/img/xiangqipieces/wikimedia/{r,b}{A,B,C,K,N,P,R}.svg`
- 许可全文见同目录 `LICENSE.md`（Copyright 2013 Chris Oakman；Copyright 2018-2020 lengyanyu258）。

棋子造型源自 Wikimedia Commons 的中国象棋棋子 SVG 素材；该仓库在 `docs/img/xiangqipieces/original/` 中保留了原始文件（`original/wikimedia/`、`original/wikipedia/`），原始作者与许可见 Commons 上对应文件页。若对外分发，请保留本说明与 `LICENSE.md`。

坐标约定：棋盘 SVG 的 `viewBox` 为 `0 0 900 1000`，九宫格交叉点位于 `x = 50 + 100 * file`、`y = 50 + 100 * rankFromTop`，与后端使用的 `a0`—`i9` 坐标一一对应。
