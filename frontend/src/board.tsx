import {useMemo} from 'react';
import {BOARD_SVG,PIECE_IMAGES} from './assets/xiangqi';

export const START_FEN='rnbakabnr/9/1c5c1/p1p1p1p1p/9/9/P1P1P1P1P/1C5C1/9/RNBAKABNR w - - 0 1';
export const GLYPH:Record<string,string>={R:'車',N:'馬',B:'相',A:'仕',K:'帥',C:'炮',P:'兵',r:'車',n:'馬',b:'象',a:'士',k:'將',c:'砲',p:'卒'};
export const PALETTE=['R','N','B','A','K','C','P','r','n','b','a','k','c','p'] as const;

const CELL=100,GRID_X=50,GRID_Y=50,PIECE_SIZE=96,WIDTH=900,HEIGHT=1000;

export type BoardProps={
  fen?:string;
  lastMove?:string|null;
  selected?:string|null;
  targets?:string[];
  onSquare?:(square:string)=>void;
  disabled?:boolean;
};

export function squarePoint(square:string){return {x:GRID_X+(square.charCodeAt(0)-97)*CELL,y:GRID_Y+(9-Number(square[1]))*CELL}}

export function squareFromPoint(vx:number,vy:number):string|null{
  const file=Math.round((vx-GRID_X)/CELL), row=Math.round((vy-GRID_Y)/CELL);
  if(file<0||file>8||row<0||row>9)return null;
  if(Math.abs(vx-(GRID_X+file*CELL))>46||Math.abs(vy-(GRID_Y+row*CELL))>46)return null;
  return `${String.fromCharCode(97+file)}${9-row}`;
}

/** FEN 局面 → Map<坐标, 棋子字母>。 */
export function parseFen(fen?:string):Map<string,string>{
  const map=new Map<string,string>();
  if(!fen)return map;
  fen.split(' ')[0].split('/').forEach((row,index)=>{
    let file=0;
    for(const token of row){
      if(/\d/.test(token))file+=+token;
      else {map.set(`${String.fromCharCode(97+file)}${9-index}`,token);file++}
    }
  });
  return map;
}

export function boardOnly(fen:string){return fen.split(' ')[0]}

/** 只保留局面与行棋方，计数位归零，避免把奇怪的历史计数传给裁判。 */
export function cleanFen(fen:string){
  const parts=fen.trim().split(/\s+/);
  return [parts[0],parts[1]==='b'?'b':'w','-','-','0','1'].join(' ');
}

export function setSquareInFen(fen:string,square:string,piece:string|null):string{
  const parts=fen.split(' ');
  const rows=boardOnly(fen).split('/').map(row=>{
    const cells:string[]=[];
    for(const token of row){
      if(/\d/.test(token))for(let i=0;i<+token;i++)cells.push('');
      else cells.push(token);
    }
    while(cells.length<9)cells.push('');
    return cells;
  });
  const file=square.charCodeAt(0)-97, row=9-Number(square[1]);
  if(rows[row]&&rows[row][file]!==undefined)rows[row][file]=piece??'';
  const text=rows.map(cells=>{
    let out='',empty=0;
    for(const cell of cells){if(cell){if(empty){out+=empty;empty=0}out+=cell}else empty++}
    return out+(empty||'');
  }).join('/');
  return [text,parts[1]??'w',...(parts.length>2?parts.slice(2):['-','-','0','1'])].join(' ');
}

export function setSideToMove(fen:string,side:'w'|'b'):string{
  const parts=fen.split(' ');
  parts[1]=side;
  return parts.join(' ');
}

/** 棋盘：xiangqiboardjs 的 900×1000 棋盘 SVG，棋子落在交叉点上。 */
export function Board({fen,lastMove,selected,targets=[],onSquare,disabled}:BoardProps){
  const pieces=useMemo(()=>parseFen(fen),[fen]);
  const marks=useMemo(()=>{
    if(!lastMove)return [] as {x:number;y:number}[];
    return [lastMove.slice(0,2),lastMove.slice(2,4)].map(squarePoint);
  },[lastMove]);
  const handleClick=onSquare&&!disabled?((event:React.MouseEvent<SVGSVGElement>)=>{
    const rect=event.currentTarget.getBoundingClientRect();
    const square=squareFromPoint((event.clientX-rect.left)/rect.width*WIDTH,(event.clientY-rect.top)/rect.height*HEIGHT);
    if(square)onSquare(square);
  }):undefined;
  return <div className="board-wrap"><svg className={`board-svg${onSquare&&!disabled?' playable':''}`} viewBox={`0 0 ${WIDTH} ${HEIGHT}`} role="img" aria-label="中国象棋棋盘" onClick={handleClick}>
    <image href={BOARD_SVG} x="0" y="0" width={WIDTH} height={HEIGHT}/>
    {marks.map((mark,index)=><circle key={`hint-${index}`} className="hint" cx={mark.x} cy={mark.y} r="46"/>)}
    {selected&&(()=>{const point=squarePoint(selected);return <circle className="selected" cx={point.x} cy={point.y} r="46"/>})()}
    {targets.map(square=>{
      const point=squarePoint(square), occupied=pieces.has(square);
      return <circle key={`target-${square}`} className={occupied?'target capture':'target'} cx={point.x} cy={point.y} r={occupied?44:16}/>;
    })}
    {Array.from(pieces.entries()).map(([square,piece])=>{
      const point=squarePoint(square);
      return <image key={square} href={PIECE_IMAGES[piece]} x={point.x-PIECE_SIZE/2} y={point.y-PIECE_SIZE/2} width={PIECE_SIZE} height={PIECE_SIZE}><title>{GLYPH[piece]}</title></image>;
    })}
  </svg></div>;
}

/** 棋子图片，用于摆棋调色板。 */
export function PieceIcon({piece,size=34}:{piece:string;size?:number}){
  return <img src={PIECE_IMAGES[piece]} width={size} height={size} alt={GLYPH[piece]} title={GLYPH[piece]}/>;
}
