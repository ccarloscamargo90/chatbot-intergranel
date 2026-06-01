import { Module } from '@nestjs/common';
import { ConfigModule } from '@nestjs/config';
import { PrismaModule } from '../../prisma/prisma.module';
import { BotApiKeyGuard } from './bot-api-key.guard';
import { BotController } from './bot.controller';
import { BotService } from './bot.service';

// Importa este módulo en app.module.ts.
// Ajusta PrismaModule/PrismaService a como estén expuestos en tu proyecto.
@Module({
  imports: [ConfigModule, PrismaModule],
  controllers: [BotController],
  providers: [BotService, BotApiKeyGuard],
})
export class BotModule {}
