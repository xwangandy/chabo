import { createBrowserRouter, Navigate } from "react-router-dom";
import { AppLayout } from "./layout/AppLayout";
import { AdminDashboard } from "../routes/admin/AdminDashboard";
import { AdvertiserDashboard } from "../routes/advertiser/AdvertiserDashboard";
import { PublisherDashboard } from "../routes/publisher/PublisherDashboard";
import { AdminLogin, Login } from "../routes/auth/DevLogin";
import { MagicLogin } from "../routes/auth/MagicLogin";
import { RequirePortal } from "../shared/auth/RequirePortal";

export const router = createBrowserRouter([
  {
    path: "/login",
    element: <Login />
  },
  {
    path: "/login/admin",
    element: <AdminLogin />
  },
  {
    path: "/login/magic",
    element: <MagicLogin />
  },
  {
    path: "/",
    element: <AppLayout />,
    children: [
      { index: true, element: <Navigate to="/advertiser" replace /> },
      {
        path: "admin",
        element: (
          <RequirePortal portal="admin">
            <AdminDashboard />
          </RequirePortal>
        )
      },
      {
        path: "advertiser",
        element: (
          <RequirePortal portal="advertiser">
            <AdvertiserDashboard />
          </RequirePortal>
        )
      },
      {
        path: "publisher",
        element: (
          <RequirePortal portal="publisher">
            <PublisherDashboard />
          </RequirePortal>
        )
      }
    ]
  }
]);
