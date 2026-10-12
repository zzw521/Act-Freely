/**
 * 应用内快捷键的真实注册（挂在 MainLayout 上，全局生效）
 *
 * 组合来自设置台账 `shortcuts`（用户在快捷键页录制改键的结果），未改键用出厂默认。
 * 只有这里登记了处理函数的能力才算「已注册」——快捷键页据此显示真实状态，
 * 没有实现的能力不会被注册成一条按了没反应的组合。
 */

import { useEffect, useRef } from 'react';
import { useNavigate } from 'react-router-dom';
import { useAppStore } from '../stores/useAppStore';
import { useWorkbenchStore } from '../store/workbenchStore';
import { useAppSettings } from './use-app-settings';
import {
  dispatchShortcut,
  effectiveCombos,
  type ShortcutOverrides,
  type TargetLike,
} from './shortcuts';

export type ShortcutHandler = () => void;

/** 在会话列表里前后循环（Ctrl+Tab 只在两个以上会话时才有意义） */
function cycleConversation(step: number): boolean {
  const s = useWorkbenchStore.getState();
  const ids = s.convOrder.length ? s.convOrder : Object.keys(s.convs);
  if (ids.length < 2) return false;
  const current = Math.max(0, ids.indexOf(s.activeId ?? ''));
  const next = ids[(current + step + ids.length) % ids.length];
  s.switchConversation(next);
  return true;
}

/**
 * 已实现的动作表。
 * key 必须与 `SHORTCUT_ACTIONS` 的 id 对齐；缺项表示能力尚未提供，
 * 对应组合不会注册（页面显示「未注册」而不是假装可用）。
 */
function buildHandlers(navigate: (path: string) => void): Record<string, ShortcutHandler> {
  return {
    openSettings: () => navigate('/settings'),
    toggleSidebar: () => useAppStore.getState().toggleSidebar(),
    toggleTaskMonitor: () => useAppStore.getState().toggleTaskMonitor(),
    newTask: () => {
      useWorkbenchStore.getState().newConversation();
      navigate('/');
    },
    cycleTask: () => {
      cycleConversation(1);
    },
    searchAllTasks: () => useAppStore.getState().setTaskSearchOpen(true),
    searchInTask: () => {
      // 查找条只在会话内有意义：不在工作台时先回到工作台
      navigate('/');
      useAppStore.getState().setFindBarOpen(true);
    },
  };
}

/** 当前已注册（有真实处理函数）的能力 id 清单 */
export function registeredShortcutIds(): string[] {
  return Object.keys(buildHandlers(() => undefined));
}

/** 挂载全局监听，返回当前生效组合与已注册清单（页面展示用） */
export function useAppShortcuts(): {
  combos: Record<string, string>;
  registered: string[];
} {
  const navigate = useNavigate();
  const [settings] = useAppSettings();
  const combos = effectiveCombos((settings.shortcuts as ShortcutOverrides | undefined) ?? null);
  const handlers = buildHandlers(navigate);

  const combosRef = useRef(combos);
  combosRef.current = combos;
  const handlersRef = useRef(handlers);
  handlersRef.current = handlers;

  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      dispatchShortcut(
        e,
        combosRef.current,
        handlersRef.current,
        e.target as TargetLike | null,
      );
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, []);

  return { combos, registered: Object.keys(handlers) };
}
