// 开源棋盘与棋子素材：lengyanyu258/xiangqiboardjs（MIT）
// 详见同目录 SOURCE.md 与 LICENSE.md。
import boardSvg from './board.svg';
import rA from './rA.svg';
import rB from './rB.svg';
import rC from './rC.svg';
import rK from './rK.svg';
import rN from './rN.svg';
import rP from './rP.svg';
import rR from './rR.svg';
import bA from './bA.svg';
import bB from './bB.svg';
import bC from './bC.svg';
import bK from './bK.svg';
import bN from './bN.svg';
import bP from './bP.svg';
import bR from './bR.svg';

export const BOARD_SVG = boardSvg;

/** FEN 字母 → 棋子图片。大写为红方，小写为黑方。 */
export const PIECE_IMAGES: Record<string, string> = {
  R: rR, N: rN, B: rB, A: rA, K: rK, C: rC, P: rP,
  r: bR, n: bN, b: bB, a: bA, k: bK, c: bC, p: bP,
};
