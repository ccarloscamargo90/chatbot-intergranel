import { HttpModule } from '@nestjs/axios';
import { Module } from '@nestjs/common';
import { ConfigModule } from '@nestjs/config';
import { PrismaModule } from '../../prisma/prisma.module';
import { BotApiKeyGuard } from './bot-api-key.guard';
import { BotComprasController } from './bot-compras.controller';
import { BotComprasService } from './bot-compras.service';
import { BotController } from './bot.controller';
import { BotInventarioController } from './bot-inventario.controller';
import { BotInventarioService } from './bot-inventario.service';
import { BotInventoryAlertEmitter } from './bot-inventory-alert.emitter';
import { BotService } from './bot.service';
import { BotVentasController } from './bot-ventas.controller';
import { BotVentasService } from './bot-ventas.service';

// Importa este módulo en app.module.ts.
// Ajusta PrismaModule/PrismaService a como estén expuestos en tu proyecto.
// HttpModule se usa solo por el emisor de alertas de inventario (saliente).
@Module({
  imports: [ConfigModule, PrismaModule, HttpModule],
  controllers: [
    BotController, // ordenes (Soporte)
    BotVentasController, // precios, cotizaciones, solicitudes (Ventas)
    BotComprasController, // OC y proveedores (Compras)
    BotInventarioController, // inventario (Inventario)
  ],
  providers: [
    BotApiKeyGuard,
    BotService,
    BotVentasService,
    BotComprasService,
    BotInventarioService,
    BotInventoryAlertEmitter,
  ],
  // Exporta el emisor para que otros módulos del ERP (los que mueven stock)
  // puedan disparar alertas de inventario hacia el chatbot.
  exports: [BotInventoryAlertEmitter],
})
export class BotModule {}
