import { useLocation, useNavigate } from "react-router-dom";

export function useHashTab<T extends string>(defaultKey: T, allowedKeys: readonly T[]) {
  const location = useLocation();
  const navigate = useNavigate();
  const hashKey = decodeURIComponent(location.hash.replace(/^#/, "")) as T;
  const activeKey = allowedKeys.includes(hashKey) ? hashKey : defaultKey;

  const setActiveKey = (key: T) => {
    navigate(`${location.pathname}${key === defaultKey ? "" : `#${encodeURIComponent(key)}`}`, { replace: true });
  };

  return [activeKey, setActiveKey] as const;
}
