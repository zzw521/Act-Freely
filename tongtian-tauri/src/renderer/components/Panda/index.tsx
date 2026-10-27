/**
 * 吉祥物「通通」
 *
 * 位图表情（由参考图统一生成，五个状态同一角色同一画风）：
 * idle / thinking / working / done / oops，透明底，任意尺寸等比缩放。
 * 动效类名定义在 styles/globals.css（.tt-panda-*），
 * prefers-reduced-motion 下统一关闭。
 */

import React from 'react';
import idleImg from '../../assets/panda/idle.png';
import thinkingImg from '../../assets/panda/thinking.png';
import workingImg from '../../assets/panda/working.png';
import doneImg from '../../assets/panda/done.png';
import oopsImg from '../../assets/panda/oops.png';

export type PandaState = 'idle' | 'thinking' | 'working' | 'done' | 'oops';
export type PandaVariant = 'head' | 'full';

export interface PandaProps {
  state?: PandaState;
  /** 正面宽度（px） */
  size?: number;
  /** head：头像/状态灯；full：空态主视觉（略大并带脚下软影） */
  variant?: PandaVariant;
  /** 脚下椭圆软影（坐姿主视觉用） */
  ground?: boolean;
  /** 关闭动效（截图、打印、静态场景用；系统 reduced-motion 另行处理） */
  animated?: boolean;
  /** 传了就作为可读图片暴露给读屏；不传按装饰元素隐藏 */
  ariaLabel?: string;
}

const STATE_IMAGE: Record<PandaState, string> = {
  idle: idleImg,
  thinking: thinkingImg,
  working: workingImg,
  done: doneImg,
  oops: oopsImg,
};

const SWAY: Record<PandaState, string | null> = {
  idle: 'tt-panda-sway',
  thinking: 'tt-panda-tilt',
  working: 'tt-panda-sway',
  done: 'tt-panda-pop',
  oops: 'tt-panda-shake',
};

const Panda: React.FC<PandaProps> = ({
  state = 'idle',
  size = 32,
  variant = 'head',
  ground = false,
  animated = true,
  ariaLabel,
}) => {
  const full = variant === 'full';
  const imgSize = full ? Math.round(size * 1.08) : size;
  const boxHeight = full ? Math.round(size * 1.14) : size;
  const motionClass = animated ? SWAY[state] : null;

  return (
    <span
      data-panda-state={state}
      data-panda-variant={variant}
      className={motionClass ?? undefined}
      role={ariaLabel ? 'img' : undefined}
      aria-label={ariaLabel}
      aria-hidden={ariaLabel ? undefined : true}
      style={{
        position: 'relative',
        display: 'inline-flex',
        alignItems: 'flex-end',
        justifyContent: 'center',
        width: size,
        height: boxHeight,
        flexShrink: 0,
      }}
    >
      {ground && (
        <span
          aria-hidden="true"
          style={{
            position: 'absolute',
            left: '10%',
            right: '10%',
            bottom: 0,
            height: Math.max(7, Math.round(size * 0.07)),
            borderRadius: '50%',
            background:
              'radial-gradient(ellipse at center, rgba(15, 23, 42, 0.20) 0%, rgba(15, 23, 42, 0.06) 58%, transparent 74%)',
          }}
        />
      )}
      <img
        src={STATE_IMAGE[state]}
        alt=""
        aria-hidden="true"
        draggable={false}
        style={{
          position: 'relative',
          display: 'block',
          width: imgSize,
          height: imgSize,
          objectFit: 'contain',
        }}
      />
    </span>
  );
};

export default Panda;
