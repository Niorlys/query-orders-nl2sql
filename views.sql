-- Amounts are integer USD cents. Sales use completion dates, never creation dates.
DROP VIEW IF EXISTS ventas_por_mes_pais;
CREATE VIEW ventas_por_mes_pais AS
SELECT
    substr(fecha_completado, 1, 7) AS anio_mes,
    país,
    COUNT(*) AS numero_pedidos,
    SUM(importe_centavos) AS importe_total_centavos,
    moneda
FROM pedidos
WHERE estado = 'completado' AND moneda = 'USD'
GROUP BY substr(fecha_completado, 1, 7), país, moneda;

DROP VIEW IF EXISTS top_10_clientes;
CREATE VIEW top_10_clientes AS
SELECT
    cliente_id,
    MIN(cliente COLLATE BINARY) AS nombre,
    COUNT(*) AS numero_pedidos,
    SUM(importe_centavos) AS importe_total_centavos,
    moneda
FROM pedidos
WHERE estado = 'completado' AND moneda = 'USD'
GROUP BY cliente_id, moneda
-- Break equal totals by order count, then by the numeric customer ID.
-- This selects the top ten; callers must also ORDER BY to guarantee display order.
ORDER BY importe_total_centavos DESC, numero_pedidos DESC, cliente_id ASC
LIMIT 10;
