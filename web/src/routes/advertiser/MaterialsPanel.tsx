import { App, Button, Form, Input, Select, Space, Table, Tag } from "antd";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { apiFetch } from "../../shared/api/client";

interface Page<T> {
  items: T[];
}

interface Material {
  id: string;
  format_type: string;
  text: string;
  target_url: string;
  button_text: string;
  status: string;
  created_at: string;
}

interface MaterialForm {
  format_type: string;
  text: string;
  target_url: string;
  button_text: string;
  light_short_text?: string;
}

export function MaterialsPanel() {
  const { message } = App.useApp();
  const [form] = Form.useForm<MaterialForm>();
  const queryClient = useQueryClient();
  const materials = useQuery({
    queryKey: ["advertiser", "materials"],
    queryFn: () => apiFetch<Page<Material>>("/api/advertiser/materials?limit=50")
  });
  const createMaterial = useMutation({
    mutationFn: (values: MaterialForm) =>
      apiFetch("/api/advertiser/materials", {
        method: "POST",
        body: JSON.stringify(values)
      }),
    onSuccess: async () => {
      message.success("素材已保存");
      form.resetFields();
      await queryClient.invalidateQueries({ queryKey: ["advertiser", "materials"] });
    }
  });
  const archiveMaterial = useMutation({
    mutationFn: (id: string) =>
      apiFetch(`/api/advertiser/materials/${id}/archive`, { method: "POST" }),
    onSuccess: async () => {
      message.success("素材已归档");
      await queryClient.invalidateQueries({ queryKey: ["advertiser", "materials"] });
    }
  });

  return (
    <Space direction="vertical" size={16} className="full-width">
      <Form<MaterialForm>
        form={form}
        layout="inline"
        initialValues={{ format_type: "standard_card", button_text: "查看详情" }}
        onFinish={(values) => createMaterial.mutate(values)}
      >
        <Form.Item name="format_type" rules={[{ required: true }]}>
          <Select
            style={{ width: 140 }}
            options={[
              { label: "文字插播", value: "light_tail" },
              { label: "标准插播", value: "standard_card" },
              { label: "定制插播", value: "strong_post" }
            ]}
          />
        </Form.Item>
        <Form.Item name="text" rules={[{ required: true }]}>
          <Input placeholder="广告文案" style={{ width: 260 }} />
        </Form.Item>
        <Form.Item name="target_url" rules={[{ required: true }]}>
          <Input placeholder="目标链接" style={{ width: 220 }} />
        </Form.Item>
        <Form.Item name="light_short_text">
          <Input placeholder="文字插播短入口" style={{ width: 160 }} />
        </Form.Item>
        <Form.Item name="button_text">
          <Input placeholder="按钮文字" style={{ width: 120 }} />
        </Form.Item>
        <Button type="primary" htmlType="submit" loading={createMaterial.isPending}>
          保存素材
        </Button>
      </Form>
      <Table<Material>
        rowKey="id"
        size="small"
        loading={materials.isLoading}
        dataSource={materials.data?.items ?? []}
        pagination={false}
        columns={[
          { title: "素材", dataIndex: "id", width: 170 },
          {
            title: "形态",
            dataIndex: "format_type",
            width: 110,
            render: (value) => <Tag>{value}</Tag>
          },
          { title: "文案", dataIndex: "text", ellipsis: true },
          { title: "目标", dataIndex: "target_url", ellipsis: true },
          {
            title: "操作",
            width: 100,
            render: (_, record) => (
              <Button
                size="small"
                danger
                loading={archiveMaterial.isPending}
                onClick={() => archiveMaterial.mutate(record.id)}
              >
                归档
              </Button>
            )
          }
        ]}
      />
    </Space>
  );
}
