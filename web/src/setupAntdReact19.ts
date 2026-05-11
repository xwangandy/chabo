import type { ReactElement } from "react";
import { unstableSetRender } from "antd";
import { createRoot, type Root } from "react-dom/client";

const roots = new WeakMap<Element | DocumentFragment, Root>();

unstableSetRender((node: ReactElement, container: Element | DocumentFragment) => {
  let root = roots.get(container);
  if (!root) {
    root = createRoot(container);
    roots.set(container, root);
  }
  root.render(node);
  return async () => {
    root?.unmount();
    roots.delete(container);
  };
});
